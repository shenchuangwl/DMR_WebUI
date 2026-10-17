#!/bin/bash

# DMR量化交易系统 - 部署文件清理脚本
# 版本: 4.7
# 功能: 同一候选算法预览/执行，项目外可恢复隔离；不实际释放文件系统块
# 保护: 八目录严格校验；不读取配置内容；排除范围内的匹配项单独列入保留
# 变更: 占用检查中管理员无权stat的FUSE挂载对象（未设allow_other，如AppImage），经挂载表确认不在项目文件系统后
#       不再误判为权限不足，并在终端与result.json列出涉及的PID；cwd/exe按路径判断时仅认FUSE，挂载表读取异常仍拒绝；
#       check/size预览不移动文件，证据目录不再受同文件系统限制；隔离执行约束及既有保护、隔离范围不变
set -euo pipefail

# 脚本入口 / 主程序逻辑：系统 Python，不依赖项目虚拟环境或当前工作目录
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
fi
exec /usr/bin/python3 -I -B - "${BASH_SOURCE[0]}" "$@" <<'DMR_PYTHON'
import argparse
import collections
import fcntl
import hashlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
from pathlib import Path

VERSION = '4.7'
KIND_LABELS = {'venv': '虚拟环境', 'cache': 'Python缓存', 'logs': '日志', 'backup': '明确备份', 'temp': '临时文件'}

class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '参数错误：命令或选项无效，或选项缺少参数。请使用 help 查看中文用法。\n')

# 保护目录列表 - 这些目录及其所有内容必须完全保留
PROTECTED = ('A_LOG', 'A_MD', 'A_PY', 'A_TEST',
             'bit/B_LOG', 'bit/B_MD', 'bit/B_PY', 'bit/B_TEST')
EXCLUDED = PROTECTED + ('.git', 'config', 'bit/config', 'data', 'bit/data', 'webui')
ROOT = Path(os.path.realpath(sys.argv[1])).parent
BASE = Path.home() / '.local' / 'state' / 'dmr-cleanup'
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_CLOEXEC
ROOTFD = None
ROOTDEV = None
MOUNTS = set()
WAIVED = set()  # 含无权stat对象、但经挂载表确认不在项目文件系统而未计为权限不足的进程

# 颜色定义 / 打印带颜色的消息：证据友好的中文纯文本

# 检查是否为保护目录（路径组件边界，不误保护同名前缀）
def inside(path, parent):
    return path == parent or path.startswith(parent + '/')

def excluded(rel):
    return any(inside(rel, p) for p in EXCLUDED)

def ancestor(rel):
    return any(inside(p, rel) for p in EXCLUDED)

def metadata(s):
    return dict(type=stat.S_IFMT(s.st_mode), size=s.st_size, mode=s.st_mode,
                uid=s.st_uid, gid=s.st_gid, mtime_ns=s.st_mtime_ns,
                ctime_ns=s.st_ctime_ns, atime_ns=s.st_atime_ns,
                inode=s.st_ino, device=s.st_dev, nlink=s.st_nlink)

def open_dir(rel=''):
    fd = os.dup(ROOTFD)
    try:
        for part in rel.split('/') if rel else ():
            new = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def parent_fd(rel):
    parent, _, name = rel.rpartition('/')
    return open_dir(parent), name

def info(rel):
    fd, name = parent_fd(rel)
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)

def names(rel):
    fd = open_dir(rel)
    try:
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)

# 保存保护目录状态：文件和目录读取必须 O_NOATIME|O_NOFOLLOW，失败即拒绝
# 符号链接仅 lstat；不 readlink、不跟随、不改变其 atime。
def save_protected_state():
    result = {}
    def visit(rel):
        s = info(rel)
        record = metadata(s)
        if stat.S_ISREG(s.st_mode):
            fd, name = parent_fd(rel)
            try:
                f = os.open(name, FLAGS, dir_fd=fd)
                try:
                    if metadata(os.fstat(f)) != record:
                        raise RuntimeError('保护文件读取前发生变化: ' + rel)
                    h = hashlib.sha256()
                    while True:
                        block = os.read(f, 1024 * 1024)
                        if not block:
                            break
                        h.update(block)
                    if metadata(os.fstat(f)) != record:
                        raise RuntimeError('保护文件读取中发生变化: ' + rel)
                    record['sha256'] = h.hexdigest()
                finally:
                    os.close(f)
            finally:
                os.close(fd)
        elif stat.S_ISDIR(s.st_mode):
            for name in names(rel):
                visit(rel + '/' + name)
        elif not stat.S_ISLNK(s.st_mode):
            raise RuntimeError('保护目录包含不支持的特殊文件: ' + rel)
        if metadata(info(rel)) != metadata(s):
            raise RuntimeError('保护路径扫描中发生变化: ' + rel)
        result[rel] = record
    for rel in PROTECTED:
        try:
            s = info(rel)
        except FileNotFoundError:
            raise RuntimeError('保护目录缺失: ' + rel)
        if not stat.S_ISDIR(s.st_mode):
            raise RuntimeError('保护目录缺失或为符号链接: ' + rel)
        visit(rel)
    return result

# 对比保护目录状态
def compare_protected_state(before, after):
    if before != after:
        raise RuntimeError('保护目录状态发生变化；停止，保留全部证据和隔离文件')

# 显示帮助信息
def show_help():
    print('DMR量化交易系统 - 部署文件清理脚本 v' + VERSION)
    print('用法: cleanup_deployment_files.sh [all|venv|cache|logs|backup|temp|check|size|help] [--yes] [--evidence-dir 新外部目录]')
    print('all 完整清理(含虚拟环境)；venv 虚拟环境；cache Python缓存；logs 日志；backup 备份；temp 临时文件')
    print('check 检查候选；size 候选逻辑字节统计；help 帮助。所有清理模式默认交互确认。')
    print('严格保护: ' + ', '.join(PROTECTED))
    print('额外整树排除: .git、config、bit/config、data、bit/data（含配置备份）。源码/文档不按 backup 字样匹配。')
    print('日期备份仅匹配 *.backup.YYYYMMDD 或 *.backup.YYYY-MM-DD[_HHMMSS]；不以 backup 子串误选源码。')
    print('不跟随符号链接；已验证虚拟环境内的链接仅随目录整体移动，链接目标不变；其他候选含链接则保留。')
    print('挂载点和特殊文件拒绝隔离。venv 须有普通 pyvenv.cfg；环境内链接仅记录元数据，不读取链接目标。')
    print('所有动作仅 os.rename 到同文件系统的唯一0700外部目录；可恢复，不实际释放文件系统块。')
    print('项目与状态目录跨文件系统时：check/size 仍可预览（不移动文件）；隔离执行须以 --evidence-dir 指定项目所在文件系统上的新外部目录。')
    print('执行须能检查所有 /proc 进程 fd/mmap/exe/cwd；权限不足拒绝，可能需要管理员权限。')
    print('未设allow_other的FUSE挂载（如AppImage）内的对象连管理员也无权stat：挂载表确认其不在项目文件系统时不计为权限不足并列出PID，否则仍拒绝。')
    print('请先停止交易及其他写入进程；锁仅协调本脚本，不能阻止外部进程并发写入。')
    print('执行后保存 final-plan.json；result.json 区分完成、未完整、取消、预览和失败状态。')
    print('all/venv/backup遇到对应环境缺少普通pyvenv.cfg时保留整目录并记录跳过，不宣称完整成功。')

# 清理虚拟环境
def venv_kind(rel, s):
    if not stat.S_ISDIR(s.st_mode):
        return None
    name = rel.rsplit('/', 1)[-1]
    kind = 'venv' if rel in ('venv', '.venv') else 'backup' if name.startswith('venv_backup_') else None
    if kind:
        try:
            if stat.S_ISREG(info(rel + '/pyvenv.cfg').st_mode):
                return kind
        except FileNotFoundError:
            pass
    return None

# 清理缓存文件 / 清理日志文件 / 清理备份文件 / 清理临时文件
def classify(rel, s):
    name = rel.rsplit('/', 1)[-1]
    if stat.S_ISDIR(s.st_mode):
        return venv_kind(rel, s) or ('cache' if name == '__pycache__' else None)
    if not stat.S_ISREG(s.st_mode) or name.endswith(('.py', '.sh', '.md')):
        return None
    if name.endswith(('.pyc', '.pyo')):
        return 'cache'
    if name.endswith('.log') or name == 'trading_log.txt':
        return 'logs'
    dated_backup = r'.+\.backup\.(?:\d{8}|\d{4}-\d{2}-\d{2})(?:[_.-]\d{6})?'
    if name.endswith(('.bak', '.backup')) or re.fullmatch(dated_backup, name):
        return 'backup'
    if name.endswith(('.tmp', '.temp', '~')) or name in ('.DS_Store', 'Thumbs.db'):
        return 'temp'
    return None

def mount_path(rel):
    return str(ROOT / rel) in MOUNTS or info(rel).st_dev != os.fstat(ROOTFD).st_dev

def inventory(rel, kind):
    records = {}
    def visit(path):
        s = info(path)
        if excluded(path) or ancestor(path) or mount_path(path):
            raise ValueError('保护路径/挂载点')
        environment_link = stat.S_ISLNK(s.st_mode) and venv_kind(rel, info(rel)) is not None
        if not environment_link and (stat.S_ISLNK(s.st_mode) or not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode))):
            raise ValueError('符号链接/特殊文件')
        if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
            raise ValueError('硬链接')
        if kind == 'cache' and stat.S_ISREG(s.st_mode) and not path.endswith(('.pyc', '.pyo')):
            raise ValueError('缓存目录含非缓存文件')
        records[path] = metadata(s)
        if stat.S_ISDIR(s.st_mode):
            for name in names(path):
                visit(path + '/' + name)
        if metadata(info(path)) != metadata(s):
            raise RuntimeError('候选扫描中变化: ' + path)
    visit(rel)
    return records

def note_retained(mode):
    # 仅枚举保护/排除树内“本会匹配”的项，不读取文件内容、不跟随链接。
    retained = []
    def visit(rel):
        try:
            s = info(rel)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(s.st_mode) or mount_path(rel):
            return
        kind = classify(rel, s)
        if kind and (mode == 'all' or mode == kind):
            retained.append({'path': rel, 'kind': kind, 'reason': '保护或排除范围'})
        if stat.S_ISDIR(s.st_mode):
            for name in names(rel):
                visit(rel + '/' + name)
    for rel in EXCLUDED:
        visit(rel)
    return retained

# 检查可清理的文件：预览与执行共用唯一候选算法
# 显示文件大小统计：基于同一计划，不使用会更新 atime 的 du/find。
def check_files(mode):
    candidates, skipped = [], []
    def visit(parent):
        for name in names(parent):
            rel = parent + '/' + name if parent else name
            s = info(rel)
            if excluded(rel) or stat.S_ISLNK(s.st_mode) or mount_path(rel):
                continue
            kind = classify(rel, s)
            if kind and (mode == 'all' or mode == kind) and not ancestor(rel):
                try:
                    records = inventory(rel, kind)
                except ValueError as exc:
                    skipped.append({'path': rel, 'reason': str(exc)})
                    continue
                candidates.append({'path': rel, 'kind': kind, 'records': records,
                                   'bytes': sum(r['size'] for r in records.values() if r['type'] == stat.S_IFREG)})
                continue
            # 不拆分未经验证的环境，防止将其中源码/运行状态作为散件清理。
            if stat.S_ISDIR(s.st_mode) and (rel in ('venv', '.venv') or name.startswith('venv_backup_')):
                environment_kind = 'venv' if rel in ('venv', '.venv') else 'backup'
                if mode in ('all', environment_kind) and venv_kind(rel, s) is None:
                    skipped.append({'path': rel, 'reason': '环境缺少普通pyvenv.cfg，保留整目录，不拆分清理'})
                continue
            if stat.S_ISDIR(s.st_mode):
                visit(rel)
    visit('')
    return {'root': str(ROOT), 'mode': mode, 'candidates': candidates, 'skipped': skipped,
            'retained': note_retained(mode)}

# 安全删除函数：仅项目外隔离，不 rm；逐项 journal 先写意图再写完成。
def write_json(directory, name, value):
    with open(directory / name, 'x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=True, indent=2)
        f.flush()
        os.fsync(f.fileno())

def external(path):
    path = Path(os.path.abspath(path))
    if inside(str(path), str(ROOT)) or inside(str(ROOT), str(path)):
        raise RuntimeError('隔离/锁目录不能是项目内部或项目祖先')
    return path

def secure_dirs(path):
    # 逐级拒绝符号链接；不 chmod 已存在的父目录。
    current = Path('/')
    for part in path.parts[1:]:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise RuntimeError('外部路径含符号链接或非目录')
    if (os.geteuid() != 0 and path.stat().st_uid != os.geteuid()) or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise RuntimeError('外部状态目录必须属于当前用户且权限0700: ' + str(path))

# 内核挂载表：挂载ID -> (设备号, 挂载点, 文件系统类型)；只读 /proc，不访问目标文件系统。
def mount_table(path):
    table = {}
    with open(path, encoding='utf-8', errors='surrogateescape') as f:
        for line in f:
            left, right = line.split(' - ', 1)
            cols = left.split()
            major, minor = (int(x) for x in cols[2].split(':'))
            table[cols[0]] = (os.makedev(major, minor), re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), cols[4]),
                              right.split()[0])
    return table

def mount_id(path):
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.startswith('mnt_id:'):
                return line.split()[1]

# 未设 allow_other 的用户态 FUSE 挂载（如 AppImage）连管理员也无权 stat。被拒对象仅当挂载表确认其
# 不在项目所在文件系统上时才不可能是候选；无法确认（含挂载表读取异常）则返回 False，仍按权限不足拒绝。
# 进程或 fd 在检查途中已消失视为已无占用，返回 True。
def off_project(proc, entry=None, target=None):
    try:
        table = mount_table(proc + '/mountinfo')
        if entry is not None:
            found = [table[i] for i in (mount_id(proc + '/fdinfo/' + entry),) if i in table]
        else:
            # cwd/exe 没有 fdinfo，只能按路径取最深挂载点：仅认 FUSE，且同一挂载点叠挂时要求全部满足。
            hits = [m for m in table.values() if m[1] == '/' or inside(target, m[1])]
            found = [m for m in hits if len(m[1]) == max(len(h[1]) for h in hits)]
            if not all(m[2].startswith('fuse') for m in found):
                return False
    except (FileNotFoundError, ProcessLookupError):
        return True
    except (OSError, ValueError, IndexError):
        return False
    return bool(found) and all(m[0] != ROOTDEV for m in found)

# 活跃进程检查：只输出 PID/路径，不读取命令行或环境秘密。
def active_processes(plan):
    identities = {(r['device'], r['inode']) for c in plan['candidates'] for r in c['records'].values()}
    paths = [str(ROOT / c['path']) for c in plan['candidates']]
    occupied, denied = [], []
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        proc = '/proc/' + pid
        try:
            for section in ('fd',):
                for entry in os.listdir(proc + '/' + section):
                    try:
                        s = os.stat(proc + '/' + section + '/' + entry)
                        if (s.st_dev, s.st_ino) in identities:
                            occupied.append({'pid': int(pid), 'reference': section})
                    except FileNotFoundError:
                        pass
                    except PermissionError:
                        if not off_project(proc, entry=entry):
                            raise
                        WAIVED.add(int(pid))
            for section in ('cwd', 'exe'):
                try:
                    target = os.readlink(proc + '/' + section)
                    try:
                        s = os.stat(proc + '/' + section)
                        match = (s.st_dev, s.st_ino) in identities
                    except PermissionError:
                        if not off_project(proc, target=target):
                            raise
                        WAIVED.add(int(pid))
                        match = False
                    if match or any(inside(target, p) for p in paths):
                        occupied.append({'pid': int(pid), 'reference': section})
                except FileNotFoundError:
                    pass
            with open(proc + '/maps', encoding='utf-8') as f:
                for line in f:
                    cols = line.split(None, 5)
                    if len(cols) >= 5:
                        major, minor = (int(x, 16) for x in cols[3].split(':'))
                        if (os.makedev(major, minor), int(cols[4])) in identities:
                            occupied.append({'pid': int(pid), 'reference': 'mmap'})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            denied.append(int(pid))
    if occupied:
        raise RuntimeError('候选被活跃进程占用（未杀进程）: ' + json.dumps(occupied))
    if denied:
        raise RuntimeError('/proc 权限不足，无法排除占用；拒绝执行。PID: ' + json.dumps(denied))

# 清理所有部署相关文件（各单类模式使用同一事务与保护校验）
def clean_all(args, evidence):
    before, plan, moved, error = None, None, [], None
    final_plan, status = None, 'failed'
    try:
        before = save_protected_state()
        write_json(evidence, 'protected-before.json', before)
        plan = check_files('all' if args.command in ('check', 'size') else args.command)
        write_json(evidence, 'plan.json', plan)
        totals = collections.Counter()
        for candidate in plan['candidates']:
            totals[candidate['kind']] += candidate['bytes']
            print('候选: ' + json.dumps(candidate['path'], ensure_ascii=False))
        categories = '、'.join('%s=%d字节' % (KIND_LABELS[k], v) for k, v in totals.items()) or '无'
        print('候选项数: %d；逻辑字节: %d；分类: %s' % (len(plan['candidates']), sum(totals.values()), categories))
        for item in plan['skipped']:
            print('跳过: %s；原因：%s' % (json.dumps(item['path'], ensure_ascii=False), item['reason']))
        for item in plan.get('retained', []):
            print('排除范围内保留: %s；类别：%s；原因：%s' %
                  (json.dumps(item['path'], ensure_ascii=False), KIND_LABELS[item['kind']], item['reason']))
        if args.command in ('check', 'size'):
            status = 'preview'
            return
        if not args.yes:
            with open('/dev/tty', 'r+') as tty:
                tty.write('确认移入外部可恢复隔离目录？(y/N): ')
                tty.flush()
                if tty.readline().strip().lower() != 'y':
                    status = 'cancelled'
                    print('操作已取消')
                    return
        if plan['candidates']:
            active_processes(plan)
        compare_protected_state(before, save_protected_state())
        if plan != check_files(args.command):
            raise RuntimeError('候选计划发生变化；请重新预览')
        payload = evidence / 'payload'
        payload.mkdir(mode=0o700)
        pfd = os.open(payload, FLAGS | os.O_DIRECTORY)
        try:
            with open(evidence / 'journal.jsonl', 'x', encoding='utf-8') as journal:
                for i, candidate in enumerate(plan['candidates']):
                    rel = candidate['path']
                    if inventory(rel, candidate['kind']) != candidate['records']:
                        raise RuntimeError('候选执行前变化: ' + rel)
                    compare_protected_state(before, save_protected_state())
                    active_processes({'candidates': [candidate]})
                    destination = '%06d' % i
                    event = {'source': rel, 'destination': 'payload/' + destination, 'state': 'intent'}
                    journal.write(json.dumps(event) + '\n'); journal.flush(); os.fsync(journal.fileno())
                    fd, name = parent_fd(rel)
                    try:
                        if metadata(os.stat(name, dir_fd=fd, follow_symlinks=False)) != candidate['records'][rel]:
                            raise RuntimeError('候选重命名前变化: ' + rel)
                        if os.path.lexists(payload / destination):
                            raise RuntimeError('拒绝覆盖隔离文件')
                        os.rename(name, destination, src_dir_fd=fd, dst_dir_fd=pfd)
                        os.fsync(fd); os.fsync(pfd)
                    finally:
                        os.close(fd)
                    moved.append(rel)
                    event['state'] = 'moved'
                    journal.write(json.dumps(event) + '\n'); journal.flush(); os.fsync(journal.fileno())
        finally:
            os.close(pfd)
        final_plan = check_files(args.command)
        write_json(evidence, 'final-plan.json', final_plan)
        if final_plan['candidates'] or final_plan['skipped']:
            status = 'incomplete'
            raise RuntimeError('清理未完整：复扫仍有候选或安全跳过项，请检查 final-plan.json')
        status = 'completed'
    except BaseException as exc:
        error = str(exc)
    finally:
        protected_ok = False
        try:
            after = save_protected_state()
            write_json(evidence, 'protected-after.json', after)
            if before is not None:
                compare_protected_state(before, after)
                protected_ok = True
        except BaseException as exc:
            error = (error + '; ' if error else '') + str(exc)
        if error and status != 'incomplete':
            status = 'failed'
        write_json(evidence, 'result.json', {'version': VERSION, 'command': args.command,
                   'moved': moved, 'protected_ok': protected_ok, 'error': error,
                   'status': status,
                   'remaining_candidates': len(final_plan['candidates']) if final_plan is not None else None,
                   'skipped_count': len((final_plan or plan)['skipped']) if plan is not None else None,
                   'stat_denied_waived_pids': sorted(WAIVED),
                   'filesystem_blocks_freed': False})
        print('证据目录: ' + str(evidence))
        if WAIVED:
            print('含无权stat对象、经挂载表确认不在项目文件系统而未计为权限不足的进程 PID: ' + json.dumps(sorted(WAIVED)))
        if error:
            raise RuntimeError(error)
        if args.command in ('check', 'size'):
            print('预览完成，未执行隔离。保护目录前后校验一致。')
        elif status != 'cancelled':
            print('保护目录前后校验一致；已隔离 %d 项。未实际释放文件系统块。' % len(moved))

# 主程序逻辑
def main():
    global ROOTFD, ROOTDEV, MOUNTS
    def interrupted(signum, frame):
        raise RuntimeError('收到终止信号 %d；保留 journal 与隔离文件' % signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    parser = ChineseParser(add_help=False)
    parser.add_argument('command', nargs='?', default='help', choices=('all','venv','cache','logs','backup','temp','check','size','help'))
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--evidence-dir')
    parser.add_argument('-h', '--help', action='store_true')
    args = parser.parse_args(sys.argv[2:])
    if args.help or args.command == 'help':
        show_help()
        return
    with open('/proc/self/mountinfo', encoding='utf-8') as f:
        MOUNTS = {re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4]) for line in f}
    ROOTFD = os.open(ROOT, FLAGS | os.O_DIRECTORY)
    try:
        ROOTDEV = mount_table('/proc/self/mountinfo')[mount_id('/proc/self/fdinfo/%d' % ROOTFD)][0]
    except KeyError:
        raise RuntimeError('无法从内核挂载表确定项目所在文件系统；拒绝执行')
    state = external(BASE)
    secure_dirs(state)
    lockpath = state / (hashlib.sha256(str(ROOT).encode()).hexdigest() + '.lock')
    lock = os.open(lockpath, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if (os.geteuid() != 0 and os.fstat(lock).st_uid != os.geteuid()) or os.fstat(lock).st_nlink != 1:
            raise RuntimeError('锁文件所有权/链接数异常')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 预览只写证据JSON、不移动文件，可跨文件系统；隔离依赖同文件系统原子重命名。
        preview = args.command in ('check', 'size')
        if args.evidence_dir:
            evidence = external(args.evidence_dir)
            # 只允许已存在且没有符号链接的父链；最终目录必须全新。
            for parent in (evidence.parent, *evidence.parent.parents):
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise RuntimeError('证据目录父链含符号链接/非目录')
            if not preview and evidence.parent.stat().st_dev != os.fstat(ROOTFD).st_dev:
                raise RuntimeError('隔离目录必须与项目同一文件系统')
            evidence.mkdir(mode=0o700)
        else:
            if not preview and state.stat().st_dev != os.fstat(ROOTFD).st_dev:
                raise RuntimeError('默认状态目录跨文件系统；隔离执行请用 --evidence-dir 指定项目所在文件系统上的新外部目录')
            evidence = Path(tempfile.mkdtemp(prefix='run-', dir=state))
        os.chmod(evidence, 0o700)
        clean_all(args, evidence)
    finally:
        os.close(lock)
        os.close(ROOTFD)

try:
    main()
except BaseException as exc:
    if isinstance(exc, SystemExit):
        raise
    print('错误：' + str(exc), file=sys.stderr)
    sys.exit(1)
DMR_PYTHON

# 版本历史:
# v4.7 增补 (2026-10-14) - 排除清单EXCLUDED加入webui：DMR_WebUI/webui子工程（独立私有仓库）的虚拟环境、运行日志与账本整树排除，不作为清理候选（WebUI计划报告5.1第5项）；版本号未变；与CLEANUP_VERIFICATION_REPORT.md同步
# v4.7 (2026-10-14) - 复核加固：无权stat对象的放行留痕（终端与result.json列出PID）；cwd/exe按路径判断时仅认FUSE挂载；
#        挂载表读取异常一律按权限不足处理；无法确定项目所在文件系统时给出明确报错；与CLEANUP_VERIFICATION_REPORT.md同步
# v4.6 (2026-10-14) - 项目迁移为 DMR_WebUI：占用检查不再把未设allow_other的FUSE挂载（AppImage）上无权stat的对象误判为权限不足（须经挂载表确认不在项目文件系统）；
#        证据目录（默认状态目录或--evidence-dir）与项目跨文件系统时 check/size 预览不再被拒，隔离执行仍要求同文件系统；本轮隔离由该版本执行
# v4.5 - 未验证环境显式安全跳过，阻止完整性误报；与CLEANUP_VERIFICATION_REPORT.md同步
# v4.4 - 执行后同算法复扫与完成状态；中文参数诊断及分类提示；与CLEANUP_VERIFICATION_REPORT.md同步
# v4.3 - 修正日期备份正则；check 列出保护/排除树内本会匹配的项；与CLEANUP_VERIFICATION_REPORT.md同步
# v4.2 - 统一候选、安全保护快照、活跃进程拒绝、外部可恢复隔离；历史空间统计不代表本次结果
# v4.1 (2025-12-16 16:35) - 执行完整清理，释放381M(96.5%)，551个缓存目录5139个.pyc文件，与CLEANUP_VERIFICATION_REPORT.md同步
# v4.0 (2025-12-16 16:25) - 执行完整清理，释放381M(96.5%)，554个缓存目录5152个.pyc文件+5M日志，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.9 (2025-12-11 14:20) - 执行完整清理，释放380.9M(98.2%)，551个缓存目录5139个.pyc文件，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.8 (2025-12-11 14:05) - 执行完整清理，更新保护目录统计(184个文件/5.2M)，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.7 (2025-12-09 10:14) - 增强稳定性，优化清理流程，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.6 (2025-12-09 09:56) - 优化保护目录统计显示，更新文件数量和大小统计，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.5 (2025-12-07 11:15) - 增加logs目录清理支持，优化日志备份机制，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.4 (2025-11-07 18:10) - 优化grep过滤机制，增强保护目录安全性，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.3 (2025-11-01 12:10) - 增强日志备份机制，优化清理逻辑，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.2 (2025-10-28 11:00) - 提升清理精度，优化提示信息，与CLEANUP_VERIFICATION_REPORT.md同步
# v3.1 (2025-10-28 10:00) - 增强8目录保护机制，添加diff验证
# v3.0 (2025-10-27) - 初始版本，基础清理功能
