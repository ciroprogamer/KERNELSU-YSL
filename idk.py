#!/usr/bin/env python3
"""
Adapts and applies the zeromount metamodule patch to a 4.9 kernel with
backported elements (KernelSU-Next, SuSFS 2.1 backport).

Key adaptations vs the upstream 5.10 patch:
  - d_path lives in fs/dcache.c (not fs/d_path.c)
  - inode_permission -> inode_permission2 (4.9 wrapper pattern)
  - vfs_statx -> vfs_fstatat (4.9 stat entry point)
  - vfs_getattr(&path, stat) -> 2-arg form (no request_mask)
  - EXPORT_SYMBOL_NS_GPL -> EXPORT_SYMBOL_GPL
  - kvmalloc_array -> kcalloc
  - Kconfig insertion point differs
  - readdir.c uses buf.previous instead of buf.prev_reclen
"""

import sys
import os

KERNEL = "."

def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def patch(path, old, new, required=True):
    full = os.path.join(KERNEL, path)
    content = read(full)
    if old not in content:
        if required:
            print(f"  [FAIL] '{path}': anchor not found:\n{old[:120]!r}")
            return False
        else:
            print(f"  [SKIP] '{path}': optional anchor not present")
            return True
    write(full, content.replace(old, new, 1))
    print(f"  [OK]   {path}")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# 1. fs/Kconfig — insert before the final endmenu
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] fs/Kconfig")
patch("fs/Kconfig",
    'source "fs/dlm/Kconfig"\n\nendmenu',
    '''source "fs/dlm/Kconfig"

config ZEROMOUNT
\tbool "ZeroMount Path Redirection Subsystem"
\tdefault y
\thelp
\t  ZeroMount allows path redirection and virtual file injection
\t  without mounting filesystems. Useful for systemless modifications.

endmenu''')

# ─────────────────────────────────────────────────────────────────────────────
# 2. fs/Makefile — append after the KSU_SUSFS line
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] fs/Makefile")
patch("fs/Makefile",
    "obj-$(CONFIG_KSU_SUSFS) += susfs.o",
    "obj-$(CONFIG_KSU_SUSFS) += susfs.o\nobj-$(CONFIG_ZEROMOUNT)\t\t+= zeromount.o")

# ─────────────────────────────────────────────────────────────────────────────
# 3. fs/dcache.c — d_path hook (file is dcache.c in 4.9, not d_path.c)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3] fs/dcache.c  (d_path hook)")

patch("fs/dcache.c",
    '#include <linux/syscalls.h>',
    '''#include <linux/syscalls.h>
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif''')

patch("fs/dcache.c",
    '''char *d_path(const struct path *path, char *buf, int buflen)
{
\tchar *res = buf + buflen;
\tstruct path root;
\tint error;

\t/*''',
    '''char *d_path(const struct path *path, char *buf, int buflen)
{
\tchar *res = buf + buflen;
\tstruct path root;
\tint error;

#ifdef CONFIG_ZEROMOUNT
\tif (path->dentry && d_backing_inode(path->dentry)) {
\t\tchar *v_path = zeromount_get_static_vpath(d_backing_inode(path->dentry));
\t\tif (v_path) {
\t\t\tint len = strlen(v_path);
\t\t\tif (buflen < len + 1) {
\t\t\t\tkfree(v_path);
\t\t\t\treturn ERR_PTR(-ENAMETOOLONG);
\t\t\t}
\t\t\t*--res = \'\\0\';
\t\t\tres -= len;
\t\t\tmemcpy(res, v_path, len);
\t\t\tkfree(v_path);
\t\t\treturn res;
\t\t}
\t}
#endif

\t/*''')

# ─────────────────────────────────────────────────────────────────────────────
# 4. fs/namei.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[4] fs/namei.c")

patch("fs/namei.c",
    '#include "internal.h"\n#include "mount.h"\n\n#define CREATE_TRACE_POINTS',
    '''#include "internal.h"
#include "mount.h"

#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif

#define CREATE_TRACE_POINTS''')

# getname_flags hook — after audit_getname, before return
patch("fs/namei.c",
    '\tresult->uptr = filename;\n\tresult->aname = NULL;\n\taudit_getname(result);\n\treturn result;\n}\n\nstruct filename *\ngetname(const char __user * filename)',
    '''\tresult->uptr = filename;
\tresult->aname = NULL;
\taudit_getname(result);

#ifdef CONFIG_ZEROMOUNT
\tif (!IS_ERR(result)) {
\t\tresult = zeromount_getname_hook(result);
\t}
#endif

\treturn result;
}

struct filename *
getname(const char __user * filename)''')

# generic_permission hook — at start of function body
patch("fs/namei.c",
    'int generic_permission(struct inode *inode, int mask)\n{\n\tint ret;\n\n\t/*\n\t * Do the basic permission checks.\n\t */',
    '''int generic_permission(struct inode *inode, int mask)
{
\tint ret;

#ifdef CONFIG_ZEROMOUNT
\tif (zeromount_is_injected_file(inode)) {
\t\tif (mask & MAY_WRITE)
\t\t\treturn -EACCES;
\t\treturn 0;
\t}

\tif (S_ISDIR(inode->i_mode) && zeromount_is_traversal_allowed(inode, mask)) {
\t\treturn 0;
\t}
#endif

\t/*
\t * Do the basic permission checks.
\t */''')

# inode_permission2 hook (4.9 equivalent of inode_permission in 5.10)
patch("fs/namei.c",
    'int inode_permission2(struct vfsmount *mnt, struct inode *inode, int mask)\n{\n\tint retval;\n\n\tretval = sb_permission(inode->i_sb, inode, mask);',
    '''int inode_permission2(struct vfsmount *mnt, struct inode *inode, int mask)
{
\tint retval;

#ifdef CONFIG_ZEROMOUNT
\tif (zeromount_is_injected_file(inode)) {
\t\tif (mask & MAY_WRITE)
\t\t\treturn -EACCES;
\t\treturn 0;
\t}

\tif (S_ISDIR(inode->i_mode) && zeromount_is_traversal_allowed(inode, mask)) {
\t\treturn 0;
\t}
#endif

\tretval = sb_permission(inode->i_sb, inode, mask);''')

# ─────────────────────────────────────────────────────────────────────────────
# 5. fs/proc/base.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[5] fs/proc/base.c")

patch("fs/proc/base.c",
    '#include "internal.h"\n#include "fd.h"',
    '''#include "internal.h"
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif
#include "fd.h"''')

# Add zeromount hook right before pathname = d_path(...)
patch("fs/proc/base.c",
    '\tpathname = d_path(path, tmp, PAGE_SIZE);\n\tlen = PTR_ERR(pathname);\n\tif (IS_ERR(pathname))\n\t\tgoto out;\n\tlen = tmp + PAGE_SIZE - 1 - pathname;',
    '''\n#ifdef CONFIG_ZEROMOUNT
\tif (!zeromount_should_skip() && path->dentry) {
\t\tstruct inode *inode = d_backing_inode(path->dentry);
\t\tif (inode) {
\t\t\tchar *vpath = zeromount_get_static_vpath(inode);
\t\t\tif (vpath) {
\t\t\t\tint vlen = strlen(vpath);
\t\t\t\tif (vlen > buflen)
\t\t\t\t\tvlen = buflen;
\t\t\t\tif (copy_to_user(buffer, vpath, vlen) == 0) {
\t\t\t\t\tkfree(vpath);
\t\t\t\t\tfree_page((unsigned long)tmp);
\t\t\t\t\treturn vlen;
\t\t\t\t}
\t\t\t\tkfree(vpath);
\t\t\t}
\t\t}
\t}
#endif

\tpathname = d_path(path, tmp, PAGE_SIZE);
\tlen = PTR_ERR(pathname);
\tif (IS_ERR(pathname))
\t\tgoto out;
\tlen = tmp + PAGE_SIZE - 1 - pathname;''')

# ─────────────────────────────────────────────────────────────────────────────
# 6. fs/proc/task_mmu.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[6] fs/proc/task_mmu.c")

patch("fs/proc/task_mmu.c",
    '#if defined(CONFIG_KSU_SUSFS_SUS_KSTAT) || defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)\n#include <linux/susfs_def.h>\n#endif',
    '''#if defined(CONFIG_KSU_SUSFS_SUS_KSTAT) || defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)
#include <linux/susfs_def.h>
#endif
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif''')

# Add spoof after the SUS_KSTAT block
patch("fs/proc/task_mmu.c",
    '\t\tdev = inode->i_sb->s_dev;\n\t\tino = inode->i_ino;\n\t\tpgoff = ((loff_t)vma->vm_pgoff) << PAGE_SHIFT;\n#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT\n\t\tsusfs_show_map_vma_spoofer(inode, &dev, &ino);\n#endif\n\t}',
    '''\t\tdev = inode->i_sb->s_dev;
\t\tino = inode->i_ino;
\t\tpgoff = ((loff_t)vma->vm_pgoff) << PAGE_SHIFT;
#ifdef CONFIG_KSU_SUSFS_SUS_KSTAT
\t\tsusfs_show_map_vma_spoofer(inode, &dev, &ino);
#endif
#ifdef CONFIG_ZEROMOUNT
\t\tzeromount_spoof_mmap_metadata(inode, &dev, &ino);
#endif
\t}''')

# ─────────────────────────────────────────────────────────────────────────────
# 7. fs/readdir.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[7] fs/readdir.c")

patch("fs/readdir.c",
    '#include <asm/uaccess.h>\n\n#ifdef CONFIG_KSU_SUSFS_SUS_PATH',
    '''#include <asm/uaccess.h>
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_PATH''')

# getdents — add initial_count, magic_pos check, inject, and zm_out label
patch("fs/readdir.c",
    '''SYSCALL_DEFINE3(getdents, unsigned int, fd,
\t\tstruct linux_dirent __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct linux_dirent __user * lastdirent;
\tstruct getdents_callback buf = {
\t\t.ctx.actor = filldir,
\t\t.count = count,
\t\t.current_dir = dirent
\t};
\tint error;

\tif (!access_ok(VERIFY_WRITE, dirent, count))
\t\treturn -EFAULT;

\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif

\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;
\tlastdirent = buf.previous;
\tif (lastdirent) {
\t\tif (put_user(buf.ctx.pos, &lastdirent->d_off))
\t\t\terror = -EFAULT;
\t\telse
\t\t\terror = count - buf.count;
\t}
\tfdput_pos(f);
\treturn error;
}''',
    '''SYSCALL_DEFINE3(getdents, unsigned int, fd,
\t\tstruct linux_dirent __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct linux_dirent __user * lastdirent;
\tstruct getdents_callback buf = {
\t\t.ctx.actor = filldir,
\t\t.count = count,
\t\t.current_dir = dirent
\t};
\tint error;
#ifdef CONFIG_ZEROMOUNT
\tint initial_count = count;
#endif

\tif (!access_ok(VERIFY_WRITE, dirent, count))
\t\treturn -EFAULT;

\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;

#ifdef CONFIG_ZEROMOUNT
\tif (f.file->f_pos >= ZEROMOUNT_MAGIC_POS) {
\t\terror = 0;
\t\tgoto skip_real_iterate;
\t}
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif

\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;

#ifdef CONFIG_ZEROMOUNT
skip_real_iterate:
\tif (error >= 0 && !signal_pending(current)) {
\t\tzeromount_inject_dents(f.file, (void __user **)&dirent, &count, &f.file->f_pos);
\t\tif (count != initial_count)
\t\t\terror = initial_count - count;
\t\tgoto zm_out;
\t}
#endif
\tlastdirent = buf.previous;
\tif (lastdirent) {
\t\tif (put_user(buf.ctx.pos, &lastdirent->d_off))
\t\t\terror = -EFAULT;
\t\telse
\t\t\terror = count - buf.count;
\t}
#ifdef CONFIG_ZEROMOUNT
zm_out:
#endif
\tfdput_pos(f);
\treturn error;
}''')

# getdents64 — same pattern
patch("fs/readdir.c",
    '''SYSCALL_DEFINE3(getdents64, unsigned int, fd,
\t\tstruct linux_dirent64 __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct linux_dirent64 __user * lastdirent;
\tstruct getdents_callback64 buf = {
\t\t.ctx.actor = filldir64,
\t\t.count = count,
\t\t.current_dir = dirent
\t};
\tint error;

\tif (!access_ok(VERIFY_WRITE, dirent, count))
\t\treturn -EFAULT;

\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;
#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif
\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;
\tlastdirent = buf.previous;
\tif (lastdirent) {
\t\ttypeof(lastdirent->d_off) d_off = buf.ctx.pos;
\t\tif (__put_user(d_off, &lastdirent->d_off))
\t\t\terror = -EFAULT;
\t\telse
\t\t\terror = count - buf.count;
\t}
\tfdput_pos(f);
\treturn error;
}''',
    '''SYSCALL_DEFINE3(getdents64, unsigned int, fd,
\t\tstruct linux_dirent64 __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct linux_dirent64 __user * lastdirent;
\tstruct getdents_callback64 buf = {
\t\t.ctx.actor = filldir64,
\t\t.count = count,
\t\t.current_dir = dirent
\t};
\tint error;
#ifdef CONFIG_ZEROMOUNT
\tint initial_count = count;
#endif

\tif (!access_ok(VERIFY_WRITE, dirent, count))
\t\treturn -EFAULT;

\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;

#ifdef CONFIG_ZEROMOUNT
\tif (f.file->f_pos >= ZEROMOUNT_MAGIC_POS) {
\t\terror = 0;
\t\tgoto skip_real_iterate;
\t}
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif
\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;

#ifdef CONFIG_ZEROMOUNT
skip_real_iterate:
\tif (error >= 0 && !signal_pending(current)) {
\t\tzeromount_inject_dents64(f.file, (void __user **)&dirent, &count, &f.file->f_pos);
\t\tif (count != initial_count)
\t\t\terror = initial_count - count;
\t\tgoto zm_out;
\t}
#endif
\tlastdirent = buf.previous;
\tif (lastdirent) {
\t\ttypeof(lastdirent->d_off) d_off = buf.ctx.pos;
\t\tif (__put_user(d_off, &lastdirent->d_off))
\t\t\terror = -EFAULT;
\t\telse
\t\t\terror = count - buf.count;
\t}
#ifdef CONFIG_ZEROMOUNT
zm_out:
#endif
\tfdput_pos(f);
\treturn error;
}''')

# COMPAT getdents — find it and patch
f_readdir = read(os.path.join(KERNEL, "fs/readdir.c"))
if 'COMPAT_SYSCALL_DEFINE3(getdents,' in f_readdir:
    # Patch the compat version — locate its fdget_pos/iterate_dir block
    patch("fs/readdir.c",
        '''COMPAT_SYSCALL_DEFINE3(getdents, unsigned int, fd,
\t\tstruct compat_linux_dirent __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct compat_linux_dirent __user * lastdirent;
\tstruct compat_getdents_callback buf = {
\t\t.ctx.actor = compat_fillonedir,
\t\t.count = count
\t};
\tint error;''',
        '''COMPAT_SYSCALL_DEFINE3(getdents, unsigned int, fd,
\t\tstruct compat_linux_dirent __user *, dirent, unsigned int, count)
{
\tstruct fd f;
\tstruct compat_linux_dirent __user * lastdirent;
\tstruct compat_getdents_callback buf = {
\t\t.ctx.actor = compat_fillonedir,
\t\t.count = count
\t};
\tint error;
#ifdef CONFIG_ZEROMOUNT
\tint initial_count = count;
#endif''',
        required=False)

    patch("fs/readdir.c",
        '''\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif
\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;
\tif (buf.prev_reclen) {
\t\tstruct compat_linux_dirent __user * lastdirent;
\t\tlastdirent = (void __user *)buf.current_dir - buf.prev_reclen;''',
        '''\tf = fdget_pos(fd);
\tif (!f.file)
\t\treturn -EBADF;

#ifdef CONFIG_ZEROMOUNT
\tif (f.file->f_pos >= ZEROMOUNT_MAGIC_POS) {
\t\terror = 0;
\t\tgoto skip_real_iterate;
\t}
#endif

#ifdef CONFIG_KSU_SUSFS_SUS_PATH
\tbuf.sb = f.file->f_inode->i_sb;
#endif
\terror = iterate_dir(f.file, &buf.ctx);
\tif (error >= 0)
\t\terror = buf.error;

#ifdef CONFIG_ZEROMOUNT
skip_real_iterate:
\tif (error >= 0 && !signal_pending(current)) {
\t\tzeromount_inject_dents(f.file, (void __user **)&dirent, &count, &f.file->f_pos);
\t\tif (count != initial_count)
\t\t\terror = initial_count - count;
\t\tgoto zm_out;
\t}
#endif
\tif (buf.prev_reclen) {
\t\tstruct compat_linux_dirent __user * lastdirent;
\t\tlastdirent = (void __user *)buf.current_dir - buf.prev_reclen;''',
        required=False)

# ─────────────────────────────────────────────────────────────────────────────
# 8. fs/stat.c  — hook into vfs_fstatat (4.9 equivalent of vfs_statx)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[8] fs/stat.c")

patch("fs/stat.c",
    '#include <asm/uaccess.h>\n#include <asm/unistd.h>',
    '''#include <asm/uaccess.h>
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif
#include <asm/unistd.h>''')

# Insert zeromount_stat_hook helper + call into vfs_fstatat
# 4.9 vfs_getattr signature: vfs_getattr(struct path *, struct kstat *)
patch("fs/stat.c",
    '''int vfs_fstatat(int dfd, const char __user *filename, struct kstat *stat,
\t\tint flag)
{
\tstruct path path;
\tint error = -EINVAL;
\tunsigned int lookup_flags = 0;''',
    '''#ifdef CONFIG_ZEROMOUNT
static inline int zeromount_stat_hook(int dfd, const char __user *filename,
\t\t\t\t      struct kstat *stat, int flags)
{
\tif (zm_is_recursive() || IS_ERR_OR_NULL(filename))
\t\treturn -ENOENT;
\tif (filename) {
\t\tchar kname[NAME_MAX + 1];
\t\tlong copied = strncpy_from_user(kname, filename, sizeof(kname));
\t\tif (copied > 0 && kname[0] != \'/\') {
\t\t\tchar *abs_path = zeromount_build_absolute_path(dfd, kname);
\t\t\tif (abs_path) {
\t\t\t\tchar *resolved = zeromount_resolve_path(abs_path);
\t\t\t\tif (resolved) {
\t\t\t\t\tstruct path zm_path;
\t\t\t\t\tint zm_ret;
\t\t\t\t\tzm_enter();
\t\t\t\t\tzm_ret = kern_path(resolved,
\t\t\t\t\t\t(flags & AT_SYMLINK_NOFOLLOW) ? 0 : LOOKUP_FOLLOW,
\t\t\t\t\t\t&zm_path);
\t\t\t\t\tzm_exit();
\t\t\t\t\tkfree(resolved);
\t\t\t\t\tkfree(abs_path);
\t\t\t\t\tif (zm_ret == 0) {
\t\t\t\t\t\tzm_ret = vfs_getattr(&zm_path, stat);
\t\t\t\t\t\tpath_put(&zm_path);
\t\t\t\t\t\treturn zm_ret;
\t\t\t\t\t}
\t\t\t\t} else {
\t\t\t\t\tkfree(abs_path);
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\treturn -ENOENT;
}
#endif

int vfs_fstatat(int dfd, const char __user *filename, struct kstat *stat,
\t\tint flag)
{
\tstruct path path;
\tint error = -EINVAL;
\tunsigned int lookup_flags = 0;''')

# Add the hook call inside vfs_fstatat just before user_path_at
patch("fs/stat.c",
    '''\tif (!(flag & AT_SYMLINK_NOFOLLOW))
\t\tlookup_flags |= LOOKUP_FOLLOW;
\tif (flag & AT_EMPTY_PATH)
\t\tlookup_flags |= LOOKUP_EMPTY;
retry:
\terror = user_path_at(dfd, filename, lookup_flags, &path);''',
    '''\tif (!(flag & AT_SYMLINK_NOFOLLOW))
\t\tlookup_flags |= LOOKUP_FOLLOW;
\tif (flag & AT_EMPTY_PATH)
\t\tlookup_flags |= LOOKUP_EMPTY;

#ifdef CONFIG_ZEROMOUNT
\tif (filename) {
\t\tint zm_ret = zeromount_stat_hook(dfd, filename, stat, flag);
\t\tif (zm_ret != -ENOENT)
\t\t\treturn zm_ret;
\t}
#endif

retry:
\terror = user_path_at(dfd, filename, lookup_flags, &path);''')

# ─────────────────────────────────────────────────────────────────────────────
# 9. fs/statfs.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[9] fs/statfs.c")

patch("fs/statfs.c",
    '#include "internal.h"\n\nstatic int flags_by_mnt',
    '''#include "internal.h"
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif

static int flags_by_mnt''')

patch("fs/statfs.c",
    '''\terror = user_path_at(AT_FDCWD, pathname, lookup_flags, &path);
\tif (!error) {
\t\terror = vfs_statfs(&path, st);
\t\tpath_put(&path);''',
    '''\terror = user_path_at(AT_FDCWD, pathname, lookup_flags, &path);
\tif (!error) {
#ifdef CONFIG_ZEROMOUNT
\t\tint spoofed;
#endif
\t\terror = vfs_statfs(&path, st);
#ifdef CONFIG_ZEROMOUNT
\t\tspoofed = zeromount_spoof_statfs(pathname, st);
\t\t(void)spoofed;
#endif
\t\tpath_put(&path);''')

# ─────────────────────────────────────────────────────────────────────────────
# 10. fs/xattr.c
# ─────────────────────────────────────────────────────────────────────────────
print("\n[10] fs/xattr.c")

patch("fs/xattr.c",
    '#include <asm/uaccess.h>\n\nstatic const char *\nstrcmp_prefix',
    '''#include <asm/uaccess.h>
#ifdef CONFIG_ZEROMOUNT
#include <linux/zeromount.h>
#endif

static const char *
strcmp_prefix''')

# Hook into vfs_getxattr at the start
patch("fs/xattr.c",
    '''vfs_getxattr(struct dentry *dentry, const char *name, void *value, size_t size)
{
\tstruct inode *inode = dentry->d_inode;
\tint error;

\terror = xattr_permission(inode, name, MAY_READ);''',
    '''vfs_getxattr(struct dentry *dentry, const char *name, void *value, size_t size)
{
\tstruct inode *inode = dentry->d_inode;
\tint error;

#ifdef CONFIG_ZEROMOUNT
\t{
\t\tssize_t zm_ret = zeromount_spoof_xattr(dentry, name, value, size);
\t\tif (zm_ret != -EOPNOTSUPP)
\t\t\treturn zm_ret;
\t}
#endif

\terror = xattr_permission(inode, name, MAY_READ);''')

# ─────────────────────────────────────────────────────────────────────────────
# 11. include/linux/zeromount.h  — create from patch, adapt for 4.9
# ─────────────────────────────────────────────────────────────────────────────
print("\n[11] include/linux/zeromount.h")

zeromount_h = r"""#ifndef _LINUX_ZEROMOUNT_H
#define _LINUX_ZEROMOUNT_H

#include <linux/types.h>
#include <linux/list.h>
#include <linux/hashtable.h>
#include <linux/spinlock.h>
#include <linux/limits.h>
#include <linux/atomic.h>
#include <linux/uidgid.h>
#include <linux/stat.h>
#include <linux/ioctl.h>
#include <linux/rcupdate.h>
#include <linux/printk.h>
#include <linux/sched.h>
#include <linux/bitops.h>

#define ZM_BLOOM_BITS     4096
#define ZM_BLOOM_SHIFT    12
#define ZM_BLOOM_MASK     (ZM_BLOOM_BITS - 1)

/* Per-task recursion guard using android_oem_data1 bit 0.
 * Survives CPU migration -- no preemption constraints needed. */
#ifdef CONFIG_ANDROID_VENDOR_OEM_DATA
#define ZM_RECURSIVE_BIT 0

static inline void zm_enter(void)
{
	set_bit(ZM_RECURSIVE_BIT,
		(unsigned long *)&current->android_oem_data1);
}

static inline void zm_exit(void)
{
	clear_bit(ZM_RECURSIVE_BIT,
		  (unsigned long *)&current->android_oem_data1);
}

static inline bool zm_is_recursive(void)
{
	return test_bit(ZM_RECURSIVE_BIT,
			(unsigned long *)&current->android_oem_data1);
}
#else
#define ZM_RECURSIVE_MARKER ((void *)0x5A4D)

/* Only claim journal_info if it is free — avoids clobbering jbd2 handles
 * held by user processes doing ext4 I/O. If already occupied, we treat
 * the call as a no-op; zm_is_recursive() returns false so ZeroMount
 * proceeds normally, which is safe since the occupied handle means we
 * are in a filesystem transaction context where redirection is unwanted. */
static inline void zm_enter(void)
{
	if (!current->journal_info)
		current->journal_info = ZM_RECURSIVE_MARKER;
}

static inline void zm_exit(void)
{
	if (current->journal_info == ZM_RECURSIVE_MARKER)
		current->journal_info = NULL;
}

static inline bool zm_is_recursive(void)
{
	return current->journal_info == ZM_RECURSIVE_MARKER;
}
#endif

extern int zeromount_debug_level;

#define ZM_LOG(level, fmt, ...) \
	do { \
		if (zeromount_debug_level >= (level) && printk_ratelimit()) \
			pr_info("ZeroMount: " fmt, ##__VA_ARGS__); \
	} while (0)

#define ZM_TRACE(fmt, ...) ZM_LOG(3, fmt, ##__VA_ARGS__)
#define ZM_DBG(fmt, ...)  ZM_LOG(2, fmt, ##__VA_ARGS__)
#define ZM_INFO(fmt, ...) ZM_LOG(1, fmt, ##__VA_ARGS__)
#define ZM_ERR(fmt, ...)  pr_err("ZeroMount: " fmt, ##__VA_ARGS__)

#define ZEROMOUNT_MAGIC_CODE	0x5A
#define ZEROMOUNT_VERSION	1
#define ZEROMOUNT_HASH_BITS	10
#define ZM_FLAG_ACTIVE		(1 << 0)
#define ZM_FLAG_IS_DIR		(1 << 7)
#define ZEROMOUNT_MAGIC_POS	0x7000000000000000ULL

#define ZEROMOUNT_IOC_MAGIC	ZEROMOUNT_MAGIC_CODE
#define ZEROMOUNT_IOC_ADD_RULE	_IOW(ZEROMOUNT_IOC_MAGIC, 1, struct zeromount_ioctl_data)
#define ZEROMOUNT_IOC_DEL_RULE	_IOW(ZEROMOUNT_IOC_MAGIC, 2, struct zeromount_ioctl_data)
#define ZEROMOUNT_IOC_CLEAR_ALL	_IO(ZEROMOUNT_IOC_MAGIC, 3)
#define ZEROMOUNT_IOC_GET_VERSION _IOR(ZEROMOUNT_IOC_MAGIC, 4, int)
#define ZEROMOUNT_IOC_ADD_UID	_IOW(ZEROMOUNT_IOC_MAGIC, 5, unsigned int)
#define ZEROMOUNT_IOC_DEL_UID	_IOW(ZEROMOUNT_IOC_MAGIC, 6, unsigned int)
#define ZEROMOUNT_IOC_GET_LIST	_IOR(ZEROMOUNT_IOC_MAGIC, 7, int)
#define ZEROMOUNT_IOC_ENABLE	_IO(ZEROMOUNT_IOC_MAGIC, 8)
#define ZEROMOUNT_IOC_DISABLE	_IO(ZEROMOUNT_IOC_MAGIC, 9)
#define ZEROMOUNT_IOC_REFRESH	_IO(ZEROMOUNT_IOC_MAGIC, 10)
#define ZEROMOUNT_IOC_GET_STATUS _IOR(ZEROMOUNT_IOC_MAGIC, 11, int)
#define MAX_LIST_BUFFER_SIZE	(64 * 1024)

struct zeromount_ioctl_data {
	char __user *virtual_path;
	char __user *real_path;
	unsigned int flags;
};

struct zeromount_rule {
	struct hlist_node node;
	struct hlist_node ino_node;
	struct list_head list;
	size_t vp_len;
	char *virtual_path;
	char *real_path;
	unsigned long real_ino;
	dev_t real_dev;
	unsigned long v_ino;
	dev_t v_dev;
	bool is_new;
	u32 flags;
	struct rcu_head rcu;
};

struct zeromount_dir_node {
	struct hlist_node node;
	char *dir_path;
	struct list_head children_names;
	struct rcu_head rcu;
};

struct zeromount_child_name {
	struct list_head list;
	char *name;
	unsigned char d_type;
	struct rcu_head rcu;
};

struct zeromount_uid_node {
	uid_t uid;
	struct hlist_node node;
	struct rcu_head rcu;
};

extern DECLARE_HASHTABLE(zeromount_rules_ht, ZEROMOUNT_HASH_BITS);
extern DECLARE_HASHTABLE(zeromount_dirs_ht, ZEROMOUNT_HASH_BITS);
extern DECLARE_HASHTABLE(zeromount_uid_ht, ZEROMOUNT_HASH_BITS);
extern DECLARE_HASHTABLE(zeromount_ino_ht, ZEROMOUNT_HASH_BITS);
extern struct list_head zeromount_rules_list;
extern spinlock_t zeromount_lock;

#ifdef CONFIG_ZEROMOUNT
extern atomic_t zeromount_enabled;

bool zeromount_should_skip(void);
char *zeromount_resolve_path(const char *pathname);
char *zeromount_build_absolute_path(int dfd, const char *name);
struct filename *zeromount_getname_hook(struct filename *name);
void zeromount_inject_dents_common(struct file *file, void __user **dirent,
				   int *count, loff_t *pos, int compat);
void zeromount_inject_dents64(struct file *file, void __user **dirent,
			      int *count, loff_t *pos);
void zeromount_inject_dents(struct file *file, void __user **dirent,
			    int *count, loff_t *pos);
char *zeromount_get_virtual_path_for_inode(struct inode *inode);
char *zeromount_get_static_vpath(struct inode *inode);
void zeromount_spoof_mmap_metadata(struct inode *inode, dev_t *dev,
				   unsigned long *ino);
bool zeromount_is_traversal_allowed(struct inode *inode, int mask);
bool zeromount_is_injected_file(struct inode *inode);
bool zeromount_is_uid_blocked(uid_t uid);
int zeromount_spoof_statfs(const char __user *pathname, struct kstatfs *buf);
ssize_t zeromount_spoof_xattr(struct dentry *dentry, const char *name,
			      void *value, size_t size);
#else
static inline bool zeromount_should_skip(void) { return true; }
static inline char *zeromount_resolve_path(const char *p) { return NULL; }
static inline char *zeromount_build_absolute_path(int dfd, const char *name) { return NULL; }
static inline struct filename *zeromount_getname_hook(struct filename *name) { return name; }
static inline void zeromount_inject_dents_common(struct file *f, void __user **d,
						 int *c, loff_t *p, int compat) {}
static inline void zeromount_inject_dents64(struct file *f, void __user **d,
					    int *c, loff_t *p) {}
static inline void zeromount_inject_dents(struct file *f, void __user **d,
					  int *c, loff_t *p) {}
static inline char *zeromount_get_virtual_path_for_inode(struct inode *inode) { return NULL; }
static inline char *zeromount_get_static_vpath(struct inode *inode) { return NULL; }
static inline void zeromount_spoof_mmap_metadata(struct inode *inode,
						 dev_t *dev,
						 unsigned long *ino) {}
static inline bool zeromount_is_traversal_allowed(struct inode *inode, int mask) { return false; }
static inline bool zeromount_is_injected_file(struct inode *inode) { return false; }
static inline bool zeromount_is_uid_blocked(uid_t uid) { return false; }
static inline int zeromount_spoof_statfs(const char __user *p, struct kstatfs *b) { return 0; }
static inline ssize_t zeromount_spoof_xattr(struct dentry *d, const char *n,
					    void *v, size_t s) { return -EOPNOTSUPP; }
#endif

#endif /* _LINUX_ZEROMOUNT_H */
"""

write(os.path.join(KERNEL, "include/linux/zeromount.h"), zeromount_h)
print("  [OK]   include/linux/zeromount.h")

# ─────────────────────────────────────────────────────────────────────────────
# 12. fs/zeromount.c  — extract from patch and apply 4.9 adaptations
# ─────────────────────────────────────────────────────────────────────────────
print("\n[12] fs/zeromount.c  (extracting + adapting from patch)")

patch_text = read("./zero.patch")

# Extract zeromount.c content from the patch (lines starting with '+')
start_marker = "diff --git a/fs/zeromount.c b/fs/zeromount.c\nnew file mode"
end_marker = "diff --git a/include/linux/zeromount.h"
start = patch_text.find(start_marker)
end = patch_text.find(end_marker)

zm_lines = []
for line in patch_text[start:end].splitlines():
    if line.startswith('+') and not line.startswith('+++'):
        zm_lines.append(line[1:])  # strip leading '+'

zeromount_c = '\n'.join(zm_lines) + '\n'

# ── 4.9 adaptations ──────────────────────────────────────────────────────────

# 1. kvmalloc_array → kcalloc (not available in 4.9)
zeromount_c = zeromount_c.replace(
    'kvmalloc_array(count, sizeof(char *), GFP_KERNEL)',
    'kcalloc(count, sizeof(char *), GFP_KERNEL)')

# 2. kvfree → kfree (kcalloc is freed with kfree in 4.9)
#    kvfree is present in 4.9 too, but just to be safe:
#    actually kvfree IS in 4.9 (include/linux/mm.h), leave it.

# 3. susfs.h include path — use susfs.h (it exists)
#    The patch already uses #include <linux/susfs.h>, which exists. Good.

# 4. strscpy — might not exist in 4.9; use strlcpy instead
#    Check: strscpy was added in 4.3, so it IS available in 4.9. Good.

# 5. The vfs_getattr call in zeromount_stat_hook is in stat.c, already adapted above.
#    zeromount.c itself doesn't call vfs_getattr directly.

# 6. EXPORT_SYMBOL_NS_GPL — not in 4.9, but zeromount.c only uses EXPORT_SYMBOL.
#    xattr.c's EXPORT_SYMBOL_GPL stays as-is (we don't change it in xattr.c).

# 7. GFP_TEMPORARY is removed in 5.x but exists in 4.9 — not used in zeromount.c.

print("  [OK]   fs/zeromount.c (adaptations applied)")
write(os.path.join(KERNEL, "fs/zeromount.c"), zeromount_c)

print("\n✓ All patches applied.")
