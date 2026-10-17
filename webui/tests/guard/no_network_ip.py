"""Kernel-enforced IP network denial inherited by the wrapped command and all children.

Derived from DMR_WebUI/tests/offline/no_network.py (copied unchanged next to this
file as no_network.py). Copyright DMR authors, Apache-2.0 (see DMR_WebUI/LICENSE).

Why derived: the original filter denies connect/sendto/sendmsg/sendmmsg for every
socket family. asyncio wakes its loop from worker threads by send() on an AF_UNIX
socketpair, which glibc implements with the sendto syscall, so under the original
filter an asyncio process with thread-pool work hangs forever (verified
2026-10-14, recorded in webui/docs/实施记录.md). This variant instead denies
creating any AF_INET / AF_INET6 / AF_PACKET socket, so no IP traffic of any kind is
possible, while AF_UNIX socketpairs (asyncio self-pipe) keep working.

Security boundary. Every libseccomp call below runs as its own statement and its
return code is checked with an explicit ``if``. Never put such a call inside an
``assert``: ``python -O`` (or PYTHONOPTIMIZE) strips assert statements entirely,
which removes the call along with the check and silently disables the filter.
"""
import ctypes
import errno
import os
import socket
import sys


class ScmpArgCmp(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                       ctypes.c_uint, ctypes.POINTER(ScmpArgCmp)]
lib.seccomp_load.argtypes = [ctypes.c_void_p]

SCMP_ACT_ALLOW = 0x7fff0000
DENY = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
SCMP_CMP_EQ = 4
BLOCKED_FAMILIES = [socket.AF_INET, socket.AF_INET6, socket.AF_PACKET]


def _fail(message):
    sys.stderr.write('no_network_ip: %s\n' % message)
    raise SystemExit(3)


if len(sys.argv) < 2:
    _fail('usage: no_network_ip.py <command> [args...]')

ctx = lib.seccomp_init(SCMP_ACT_ALLOW)
if not ctx:
    _fail('seccomp_init failed')

num = lib.seccomp_syscall_resolve_name(b'socket')
if num < 0:
    _fail('cannot resolve syscall socket')
for family in BLOCKED_FAMILIES:
    cmp = ScmpArgCmp(0, SCMP_CMP_EQ, int(family), 0)
    rc = lib.seccomp_rule_add_array(ctx, DENY, num, 1, ctypes.byref(cmp))
    if rc != 0:
        _fail('seccomp_rule_add_array(socket, family=%d) returned %d' % (family, rc))

rc = lib.seccomp_load(ctx)
if rc != 0:
    _fail('seccomp_load returned %d' % rc)

os.environ['DMR_WEBUI_NO_NETWORK'] = '1'
os.execvp(sys.argv[1], sys.argv[1:])
