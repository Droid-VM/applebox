# applebox

One multicall binary of Apple's own open-source command-line tools
([apple-oss-distributions](https://github.com/apple-oss-distributions)), built for iOS: busybox/toybox-style, the
command is `argv[0]` (a link named after it) or the first argument (`applebox echo hi`). `applebox --list` prints the
commands it carries.

It is the coreutils of the debug shell of [UniXNU-droidvm](https://github.com/Droid-VM/UniXNU-droidvm), the iPadOS
guest of DroidVM, beside zsh (the shell) and dropbear (the ssh server). Nothing in it is specific to that guest: the
binary runs on any iOS 17 device that trusts its CDHash.

## Commands

v1 takes the commands of `shell_cmds` (shell_cmds-216.60.1) that need libc only:

`apply basename chroot dirname echo env false getopt hexdump hostname jot kill logname mktemp nice path_helper printenv
printf pwd renice seq shlock sleep tee test [ true uname users what whereis which xargs yes`

Left out for now: `date` (protocols/timed.h), `id` (OpenDirectory), `killall` (sys/user.h), `nohup` (vproc.h),
`systime` (libproc.h), `expr` and `find` (yacc grammars), `who` and `lastcomm` (utmp/acct, absent on iOS).

## Build

On a Mac with Xcode (the iphoneos and macOS SDKs):

```sh
python3 build.py out                                   # out/applebox, out/applebox.json
python3 build.py out --entitlements debug.plist        # signed with those entitlements
```

The source is downloaded from GitHub at a pinned tag and checked against its SHA-256. Each command is compiled with
`-Dmain=<cmd>_main` and partial-linked (`ld -r`) exporting only `_<cmd>_main`, so the commands' internal globals
(usage, flags, helpers whose names collide across commands) stay private to their own object; a generated dispatcher
links them into one binary. It is signed ad hoc with 4 KiB pages; `applebox.json` records the CDHash, which the device
has to trust (an owner trust cache, for UniXNU-droidvm `make-owner-trust-bundle.py`).

## License

The build script: BSD-2-Clause-Patent (LICENSE). The commands keep the licenses of their Apple sources (BSD and APSL);
they are downloaded at build time and not part of this repository.
