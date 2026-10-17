"""Kernel-enforced network denial inherited by bash and all children.

Security boundary. Every libseccomp call below runs as its own statement and its
return code is checked with an explicit ``if``. Never put such a call inside an
``assert``: ``python -O`` (or PYTHONOPTIMIZE) strips assert statements entirely,
which removes the call along with the check and silently disables the filter.
"""
import ctypes
import errno
import os
import sys

lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_load.argtypes = [ctypes.c_void_p]

SCMP_ACT_ALLOW = 0x7fff0000
DENY = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
BLOCKED_SYSCALLS = ['connect', 'sendto', 'sendmsg', 'sendmmsg']


def _fail(message):
    sys.stderr.write('no_network: %s\n' % message)
    raise SystemExit(3)


if len(sys.argv) < 2:
    _fail('usage: no_network.py <command> [args...]')

ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
if not ctx:
    _fail('seccomp_init failed')

for name in BLOCKED_SYSCALLS:
    num = lib.seccomp_syscall_resolve_name(name.encode())
    if num < 0:
        _fail('cannot resolve syscall %s' % name)
    rc = lib.seccomp_rule_add(ctx, DENY, num, 0)
    if rc != 0:
        _fail('seccomp_rule_add(%s) returned %d' % (name, rc))

rc = lib.seccomp_load(ctx)
if rc != 0:
    _fail('seccomp_load returned %d' % rc)

os.execvp(sys.argv[1], sys.argv[1:])
