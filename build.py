#!/usr/bin/env python3
"""Build applebox: one multicall binary of Apple's own open-source command-line tools (apple-oss-distributions) for iOS,
busybox/toybox-style -- argv[0] (or `applebox CMD ...`) selects the command.

  build.py OUT_DIR [--entitlements PLIST] [--identifier ID] [--deployment-target VERSION]

Writes OUT_DIR/applebox, signed ad hoc with 4 KiB pages (and PLIST's entitlements when given), and OUT_DIR/applebox.json:
its CDHash, the commands and aliases it carries and the source it was built from. OUT_DIR/work keeps the downloaded source and
the objects. Needs Xcode with the iphoneos and macOS SDKs.

v1 draws the libc-only commands from shell_cmds. Each command is compiled with -Dmain=<cmd>_main, then partial-linked (ld -r)
exporting only _<cmd>_main, so each command's internal globals (usage, flags, helpers that collide across commands) become
private to its own object. A generated dispatcher links them together."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

SHELL_CMDS_TAG = 'shell_cmds-216.60.1'
SHELL_CMDS_URL = f'https://github.com/apple-oss-distributions/shell_cmds/archive/refs/tags/{SHELL_CMDS_TAG}.tar.gz'
SHELL_CMDS_SHA256 = 'd202bba594640fe3cd9ae320a2fc5a0260ee8e41e549361938b6d05ce02bb805'

# v1: libc-only commands from shell_cmds (the survey's dep=0 set), minus the shell (zsh covers it) and the pty/libutil ones.
# v1 set: builds and links with libc + the iphoneos SDK alone. Dropped for v1 (need a shim/yacc): date (protocols/timed.h),
# id (OpenDirectory.framework), killall (sys/user.h), nohup (vproc.h), systime (libproc.h), expr & find (yacc .y grammar +
# getdate), who & lastcomm (utmp/acct, absent on iOS).
SHELL_CMDS = [
    'apply', 'basename', 'chroot', 'dirname', 'echo', 'env', 'false', 'getopt', 'hexdump', 'hostname', 'jot', 'kill',
    'logname', 'mktemp', 'nice', 'path_helper', 'printenv', 'printf', 'pwd', 'renice', 'seq', 'shlock', 'sleep', 'tee',
    'test', 'true', 'uname', 'users', 'what', 'whereis', 'which', 'xargs', 'yes',
]
# extra argv[0] aliases -> command
ALIASES = {'[': 'test'}


def fetch(work, url, tag, sha):
    tarball = work / f'{tag}.tar.gz'
    if not tarball.is_file():
        urllib.request.urlretrieve(url, tarball)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    if sha and digest != sha:
        sys.exit(f'{tag} sha256 {digest} != pinned {sha}')
    before = {p.name for p in work.iterdir() if p.is_dir()}
    with tarfile.open(tarball) as t:
        t.extractall(work, filter='data')
    print(f'{tag} sha256 {digest}')
    after = [p for p in work.iterdir() if p.is_dir() and p.name not in before]
    return after[0] if after else work


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out', type=Path)
    parser.add_argument('--entitlements', type=Path, help='entitlements plist to sign with (default: none)')
    parser.add_argument('--identifier', default='org.droidvm.applebox', help='code signing identifier')
    parser.add_argument('--deployment-target', default='17.0', help='minimum iOS version (default 17.0)')
    args = parser.parse_args()
    out = args.out.resolve()
    work, obj = out / 'work', out / 'obj'
    for directory in (out, obj):
        directory.mkdir(parents=True, exist_ok=True)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    iphone_sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
    # shim headers the iphoneos SDK lacks: get_compat.h (COMPAT_MODE, a macOS-only unix-conformance switch -> always 0 here).
    shim = work / 'shim'
    shim.mkdir()
    (shim / 'get_compat.h').write_text(
        '#pragma once\n#include <stddef.h>\n'
        'static inline int compat_mode(const char *a __attribute__((unused)), const char *b __attribute__((unused))) { return 0; }\n'
        '#define COMPAT_MODE(a,b) compat_mode((a),(b))\n'
        '#define COMPAT_MODE_FOREACH 0\n')

    sc = fetch(work, SHELL_CMDS_URL, SHELL_CMDS_TAG, SHELL_CMDS_SHA256)
    # locate the dir that holds the command subdirs (GitHub names it <repo>-<tag>)
    scdir = sc
    if not (scdir / 'echo').is_dir():
        hit = next(sc.rglob('echo/echo.c'), None)
        if not hit:
            sys.exit('cannot find shell_cmds echo/echo.c in the extracted archive')
        scdir = hit.parents[1]
    print('shell_cmds dir:', scdir)

    # DROIDVM_IOS_TARGET (arm64e-apple-ios27.0 for iPadOS 27, which runs no arm64 platform binaries) wins over the default arm64 one.
    target = os.environ.get('DROIDVM_IOS_TARGET') or f'arm64-apple-ios{args.deployment_target}'
    cc = ['xcrun', 'clang', '-target', target, '-isysroot', iphone_sdk,
          f'-I{shim}', '-O2', '-fno-common', '-Wno-everything',
          '-D__DARWIN_UNIX03=1', '-DBSD4_4']

    built, failed = [], {}
    cmd_objs = []
    for cmd in SHELL_CMDS:
        cdir = scdir / cmd
        srcs = sorted(cdir.glob('*.c'))
        if not srcs:
            failed[cmd] = 'no sources'
            continue
        objs = []
        err = None
        for src in srcs:
            o = obj / f'{cmd}__{src.stem}.o'
            # rename only the entry point; -Dmain is applied to every file (only the one with main() is affected)
            r = subprocess.run(cc + [f'-Dmain={cmd}_main', f'-I{cdir}', '-c', str(src), '-o', str(o)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'compile failed'
                break
            objs.append(o)
        if err:
            failed[cmd] = err
            continue
        # partial-link the command, exporting only its entry point so internal globals cannot collide across commands
        merged = obj / f'{cmd}.cmd.o'
        expo = obj / f'{cmd}.exp'
        expo.write_text(f'_{cmd}_main\n')
        r = subprocess.run(['ld', '-r', '-exported_symbols_list', str(expo), *map(str, objs), '-o', str(merged)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failed[cmd] = 'ld -r: ' + (r.stderr.strip().splitlines()[-1] if r.stderr.strip() else '?')
            continue
        cmd_objs.append(merged)
        built.append(cmd)

    print(f'built {len(built)}/{len(SHELL_CMDS)}:', ' '.join(built))
    if failed:
        print('FAILED:')
        for k, v in failed.items():
            print(f'  {k}: {v}')

    # dispatcher
    disp = work / 'applebox_main.c'
    decls = '\n'.join(f'extern int {c}_main(int, char **);' for c in built)
    table = '\n'.join(f'    {{"{c}", {c}_main}},' for c in built)
    alias_rows = '\n'.join(f'    {{"{a}", {t}_main}},' for a, t in ALIASES.items() if t in built)
    disp.write_text(f'''#include <libgen.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
{decls}
static const struct {{ const char *name; int (*fn)(int, char **); }} kCommands[] = {{
{table}
{alias_rows}
}};
static int list(void) {{
    for (unsigned i = 0; i < sizeof(kCommands) / sizeof(kCommands[0]); i++) printf("%s\\n", kCommands[i].name);
    return 0;
}}
int main(int argc, char **argv) {{
    char path[1024];
    strlcpy(path, argc > 0 ? argv[0] : "applebox", sizeof path);
    const char *name = basename(path);
    if (!strcmp(name, "applebox")) {{
        if (argc < 2 || !strcmp(argv[1], "--list")) return list();
        argv++; argc--;
        strlcpy(path, argv[0], sizeof path);
        name = basename(path);
    }}
    for (unsigned i = 0; i < sizeof(kCommands) / sizeof(kCommands[0]); i++)
        if (!strcmp(name, kCommands[i].name)) return kCommands[i].fn(argc, argv);
    fprintf(stderr, "applebox: no command '%s' (try: applebox --list)\\n", name);
    return 127;
}}
''')
    disp_o = obj / 'applebox_main.o'
    subprocess.run(cc + ['-c', str(disp), '-o', str(disp_o)], check=True)

    binary = out / 'applebox'
    subprocess.run(['xcrun', 'clang', '-target', target, '-isysroot', iphone_sdk,
                    str(disp_o), *map(str, cmd_objs), '-o', str(binary)], check=True)

    sign = ['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', args.identifier]
    if args.entitlements:
        sign += ['--entitlements', str(args.entitlements.resolve())]
    subprocess.run(sign + [str(binary)], check=True)
    detail = subprocess.run(['codesign', '--display', '--verbose=4', str(binary)], capture_output=True, text=True,
                            check=True).stderr
    assert re.search(r'^Page size=4096$', detail, re.M), 'the signature must use 4 KiB pages'
    cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', detail, re.M)[1]

    (out / 'applebox.json').write_text(json.dumps(
        {'cdhash': cdhash, 'binary': str(binary), 'commands': built, 'aliases': ALIASES, 'identifier': args.identifier,
         'entitlements': str(args.entitlements) if args.entitlements else None,
         'source': f'apple-oss-distributions {SHELL_CMDS_TAG}, iphoneos {target.split("-")[0]}'}, indent=2) + '\n')
    print('applebox CDHash', cdhash, 'size', binary.stat().st_size)


if __name__ == '__main__':
    main()
