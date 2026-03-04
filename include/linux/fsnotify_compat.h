/* SPDX-License-Identifier: GPL-2.0
 *
 * fsnotify_compat.h
 *
 * Backports the post-4.9 fsnotify API to a 4.9 kernel.
 * Include this at the top of any file that was written against the new API.
 * Zero changes to that file are needed beyond the #include.
 *
 * What is fixed, and how:
 *
 *  ┌─────────────────────────────┬────────────────────────────────────────────┐
 *  │ Problem                     │ Fix                                        │
 *  ├─────────────────────────────┼────────────────────────────────────────────┤
 *  │ struct fsnotify_iter_info   │ Empty stub defined here                    │
 *  │ does not exist on 4.9       │                                            │
 *  ├─────────────────────────────┼────────────────────────────────────────────┤
 *  │ handle_event in             │ struct fsnotify_ops is redefined under a   │
 *  │ fsnotify_ops has old type:  │ new name (__fsnotify_ops_compat) with the  │
 *  │  - void *data (not const)   │ corrected handle_event type, then          │
 *  │  - no iter_info arg         │ #define fsnotify_ops __fsnotify_ops_compat  │
 *  │                             │ Memory layout is identical; the kernel can │
 *  │                             │ use a pointer to our struct transparently. │
 *  │                             │ iter_info is always NULL at runtime on 4.9 │
 *  │                             │ so callers that guard with NULL-check are  │
 *  │                             │ safe; callers that ignore it are also safe.│
 *  ├─────────────────────────────┼────────────────────────────────────────────┤
 *  │ fsnotify_init_mark(mark,    │ Macro shadows the kernel symbol.           │
 *  │   group)                    │ Saves group ptr; forwards to 4.9 real impl │
 *  │ vs 4.9: (mark, free_mark)   │ with a no-op free_mark callback.           │
 *  ├─────────────────────────────┼────────────────────────────────────────────┤
 *  │ fsnotify_add_mark(mark,     │ Macro shadows the kernel symbol.           │
 *  │   inode, mnt, allow_dups)   │ Re-injects the group saved by init_mark.   │
 *  │ vs 4.9: (mark, group, ...)  │                                            │
 *  ├─────────────────────────────┼────────────────────────────────────────────┤
 *  │ -Wmissing-braces on {0}     │ Suppressed for the including file via      │
 *  │ struct initialisers         │ #pragma GCC diagnostic ignored             │
 *  └─────────────────────────────┴────────────────────────────────────────────┘
 */

#ifndef _FSNOTIFY_COMPAT_H
#define _FSNOTIFY_COMPAT_H

#include <linux/version.h>

#if LINUX_VERSION_CODE <= KERNEL_VERSION(4, 9, 999)

#include <linux/fsnotify_backend.h>

/* =========================================================================
 * 1. struct fsnotify_iter_info — stub
 *
 * This struct was introduced after 4.9.  We define an empty stub here so
 * that any function with it in its signature compiles.  At runtime on this
 * kernel, every pointer to this type will be NULL; always guard before use.
 * ====================================================================== */
struct fsnotify_iter_info {
	/* 4.9 stub — no fields */
};

/* Companion helpers that code may reference — all no-ops on 4.9 */
static inline struct fsnotify_mark *
fsnotify_iter_inode_mark(struct fsnotify_iter_info *i)    { return NULL; }

static inline struct fsnotify_mark *
fsnotify_iter_vfsmount_mark(struct fsnotify_iter_info *i) { return NULL; }

static inline int
fsnotify_iter_should_report_type(struct fsnotify_iter_info *i, int t) { return 1; }


/* =========================================================================
 * 2. struct fsnotify_ops — redefined with new handle_event type
 *
 * The 4.9 handle_event type is:
 *   int (*)(group, inode, inode_mark, vfsmount_mark,
 *           mask, void *data, data_type, file_name, cookie)
 *
 * The new type adds `const` to data and appends iter_info:
 *   int (*)(group, inode, inode_mark, vfsmount_mark,
 *           mask, const void *data, data_type, file_name, cookie,
 *           struct fsnotify_iter_info *)
 *
 * We cannot modify the already-defined struct fsnotify_ops, but we can
 * define a new struct with identical field names and layout, and redirect
 * all uses to it via macro.  Because the only difference is the pointed-to
 * function type — not the pointer width — the memory layout is byte-for-byte
 * identical.  The kernel holds a `struct fsnotify_ops *` and calls
 * ops->handle_event(...) using the old 9-arg type; it reaches our struct
 * through a compatible pointer and invokes the correct function.
 *
 * Note on iter_info at runtime: the 4.9 dispatch site calls handle_event
 * with 9 arguments.  Our function declares a 10th (iter_info).  On all
 * common ABI targets (ARM32, ARM64, x86_64) the 10th argument slot will
 * contain an indeterminate value — treat it exactly like NULL and never
 * dereference it without a NULL guard, which the new API already requires.
 * ====================================================================== */
struct __fsnotify_ops_compat {
	int (*handle_event)(struct fsnotify_group *group,
			    struct inode *inode,
			    struct fsnotify_mark *inode_mark,
			    struct fsnotify_mark *vfsmount_mark,
			    u32 mask,
			    const void *data,       /* const — new API */
			    int data_type,
			    const unsigned char *file_name,
			    u32 cookie,
			    struct fsnotify_iter_info *iter_info); /* new arg */
	void (*free_group_priv)(struct fsnotify_group *group);
	void (*freeing_mark)(struct fsnotify_mark *mark,
			     struct fsnotify_group *group);
	bool (*should_send_event)(struct fsnotify_group *group,
				  struct inode *inode,
				  struct fsnotify_mark *inode_mark,
				  struct fsnotify_mark *vfsmount_mark,
				  u32 mask, void *data, int data_type);
};

/*
 * Capture the real kernel type in a typedef BEFORE the macro rename below.
 * After `#define fsnotify_ops __fsnotify_ops_compat`, any reference to
 * `struct fsnotify_ops` — including inside casts — expands to the compat
 * struct, making the cast a no-op.  A typedef is not subject to macro
 * expansion so it permanently holds the original kernel type.
 */
typedef struct fsnotify_ops __fsnotify_ops_real_t;

/* Redirect: any reference to `struct fsnotify_ops` now uses our compat struct */
#define fsnotify_ops __fsnotify_ops_compat

/* =========================================================================
 * 2b. fsnotify_alloc_group(ops) + handle_event trampoline
 *
 * THE RUNTIME PROBLEM
 * -------------------
 * The 4.9 kernel calls ops->handle_event with 9 arguments.  If we let the
 * kernel hold a pointer to susfs's new-API function (which declares 10
 * parameters), the 10th parameter (iter_info) receives whatever garbage
 * happens to be in that register at the call site.  If susfs ever reads
 * iter_info without a NULL guard, that is a crash.
 *
 * THE FIX: trampoline
 * -------------------
 * We never give the kernel a pointer to susfs's function directly.
 * Instead we give it a pointer to __fsnotify_compat_trampoline, which has
 * the exact 4.9 signature (9 args, ABI-perfect).  The trampoline then
 * calls susfs's real function and explicitly passes NULL for iter_info.
 *
 * This is enforced at fsnotify_alloc_group() time, which is the single
 * point where the ops struct crosses the boundary from our code into the
 * kernel.  Steps:
 *   1. Save susfs's new-API handle_event pointer.
 *   2. memcpy the compat ops into a real __fsnotify_ops_real_t (layouts
 *      are byte-identical; only the function pointer type differs).
 *   3. Overwrite handle_event in the real ops with the trampoline.
 *   4. Hand the patched real ops to the kernel.
 *
 * iter_info is now ALWAYS NULL — not garbage — on this kernel.
 * ====================================================================== */

/* Step 1: storage for susfs's real new-API handle_event */
static int (*__fsnotify_compat_real_handle_event)(
	struct fsnotify_group *,
	struct inode *,
	struct fsnotify_mark *,
	struct fsnotify_mark *,
	u32, const void *, int,
	const unsigned char *, u32,
	struct fsnotify_iter_info *);

/* Step 2: trampoline — exact 4.9 ABI, calls real fn with NULL iter_info */
static int
__fsnotify_compat_trampoline(struct fsnotify_group *group,
			     struct inode *inode,
			     struct fsnotify_mark *inode_mark,
			     struct fsnotify_mark *vfsmount_mark,
			     u32 mask,
			     void *data,       /* 4.9: non-const */
			     int data_type,
			     const unsigned char *file_name,
			     u32 cookie)
{
	/*
	 * iter_info is explicitly NULL — never garbage.
	 * data cast to const void * is safe: same representation, we are
	 * only adding a qualifier.
	 */
	return __fsnotify_compat_real_handle_event(
		group, inode, inode_mark, vfsmount_mark,
		mask, (const void *)data, data_type,
		file_name, cookie,
		NULL);   /* <-- iter_info: guaranteed NULL, not a garbage register */
}

/* Step 3+4: patched alloc_group */
static inline struct fsnotify_group *
__fsnotify_compat_alloc_group(const struct __fsnotify_ops_compat *compat_ops)
{
	/*
	 * real_ops has the kernel's original fsnotify_ops layout.
	 * memcpy is safe because the two structs are byte-identical
	 * (every field is the same size; handle_event is a pointer in both).
	 */
	static __fsnotify_ops_real_t real_ops;
	memcpy(&real_ops, compat_ops, sizeof(real_ops));

	/* Save susfs's handle_event, then install the trampoline */
	__fsnotify_compat_real_handle_event = compat_ops->handle_event;
	real_ops.handle_event = __fsnotify_compat_trampoline;

	return fsnotify_alloc_group(&real_ops);
}

#define fsnotify_alloc_group(ops) \
	__fsnotify_compat_alloc_group(ops)


/* =========================================================================
 * 3. fsnotify_init_mark(mark, group)
 *
 * 4.9:  fsnotify_init_mark(mark, free_mark_fn)
 * New:  fsnotify_init_mark(mark, group)
 *
 * Save the group for use by fsnotify_add_mark below, then forward to the
 * real kernel function with a no-op free_mark callback.
 * ====================================================================== */
static struct fsnotify_group *__fsnotify_compat_group;

static inline void __fsnotify_compat_noop_free(struct fsnotify_mark *m) {}

static inline void
__fsnotify_compat_init_mark(struct fsnotify_mark *mark,
			    struct fsnotify_group *group)
{
	__fsnotify_compat_group = group;
	fsnotify_init_mark(mark, __fsnotify_compat_noop_free);
}

#define fsnotify_init_mark(mark, group_or_fn) \
	__fsnotify_compat_init_mark(mark, group_or_fn)


/* =========================================================================
 * 4. fsnotify_add_mark(mark, inode, mnt, allow_dups)
 *
 * 4.9:  fsnotify_add_mark(mark, group, inode, mnt, allow_dups)
 * New:  fsnotify_add_mark(mark, inode, mnt, allow_dups)
 *
 * Re-inject the group saved by init_mark.
 * ====================================================================== */
static inline int
__fsnotify_compat_add_mark(struct fsnotify_mark *mark,
			   struct inode *inode,
			   struct vfsmount *mnt,
			   int allow_dups)
{
	return fsnotify_add_mark(mark, __fsnotify_compat_group,
				 inode, mnt, allow_dups);
}

#define fsnotify_add_mark(mark, inode, mnt, allow_dups) \
	__fsnotify_compat_add_mark(mark, inode, mnt, allow_dups)


/* =========================================================================
 * 5. Suppress -Wmissing-braces for {0} struct initialisers
 *    Pushed here without a matching pop so it applies to the entire
 *    including translation unit after this point.
 * ====================================================================== */
#pragma GCC diagnostic ignored "-Wmissing-braces"

#endif /* LINUX_VERSION_CODE <= 4.9 */
#endif /* _FSNOTIFY_COMPAT_H */
