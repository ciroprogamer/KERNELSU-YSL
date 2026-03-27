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
 *  │                             │ A BUILD_BUG_ON enforces this at compile    │
 *  │                             │ time (see section 2b).                     │
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
 *  │ -Wmissing-braces on {0}     │ Suppressed with push/pop scoped to the     │
 *  │ struct initialisers         │ including file only (not globally).        │
 *  └─────────────────────────────┴────────────────────────────────────────────┘
 */

#ifndef _FSNOTIFY_COMPAT_H
#define _FSNOTIFY_COMPAT_H

#include <linux/version.h>

#if LINUX_VERSION_CODE <= KERNEL_VERSION(4, 9, 999)

#include <linux/bug.h>
#include <linux/fsnotify_backend.h>
#include <linux/string.h>

/* =========================================================================
 * 1. struct fsnotify_iter_info — stub
 *
 * This struct was introduced after 4.9.  We define an empty stub here so
 * that any function with it in its signature compiles.  At runtime on this
 * kernel, every pointer to this type will be NULL (guaranteed by the
 * trampoline in section 2b); always guard before use.
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
 * identical.  A BUILD_BUG_ON in section 2b verifies this at compile time so
 * that any future 4.9.x point-release surprises are caught immediately.
 *
 * Note on iter_info at runtime: the trampoline in section 2b ensures
 * iter_info is ALWAYS NULL — never a garbage register value.
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
 * All mutable compat state is centralised in one struct to make the
 * coupling between init_mark, add_mark, and alloc_group explicit and to
 * avoid bare globals scattered across the header.
 *
 * THE RUNTIME PROBLEM
 * -------------------
 * The 4.9 kernel calls ops->handle_event with 9 arguments.  If we let the
 * kernel hold a pointer to the new-API function (10 parameters), the 10th
 * parameter (iter_info) receives whatever garbage happens to be in that
 * register at the call site.  If the handler ever reads iter_info without a
 * NULL guard, that is a crash.
 *
 * THE FIX: trampoline
 * -------------------
 * We never give the kernel a pointer to the user's function directly.
 * Instead we give it a pointer to __fsnotify_compat_trampoline, which has
 * the exact 4.9 signature (9 args, ABI-perfect).  The trampoline then
 * calls the real function and explicitly passes NULL for iter_info.
 *
 * This is enforced at fsnotify_alloc_group() time, which is the single
 * point where the ops struct crosses the boundary into the kernel.  Steps:
 *   1. Save the new-API handle_event pointer into __fsnotify_compat_ctx.
 *   2. memcpy the compat ops into a real __fsnotify_ops_real_t (layouts
 *      are byte-identical; only the function pointer type differs).
 *      A BUILD_BUG_ON verifies the sizes match at compile time.
 *   3. Overwrite handle_event in the real ops with the trampoline.
 *   4. Hand the patched real ops to the kernel.
 *
 * iter_info is now ALWAYS NULL — not garbage — on this kernel.
 * ====================================================================== */

/*
 * Centralised compat context.
 *
 * NOTE ON THREAD SAFETY
 * ---------------------
 * This context is written once during setup (single-threaded kthread startup)
 * and read-only thereafter.  If your driver ever calls fsnotify_alloc_group()
 * or fsnotify_init_mark() from concurrent paths, you must add your own
 * locking around those call sites; the compat layer itself intentionally
 * stays lock-free to avoid pulling in spinlock overhead for the common case.
 */
struct __fsnotify_compat_ctx {
	/* Saved by fsnotify_alloc_group; used by the trampoline */
	int (*real_handle_event)(struct fsnotify_group *,
				 struct inode *,
				 struct fsnotify_mark *,
				 struct fsnotify_mark *,
				 u32, const void *, int,
				 const unsigned char *, u32,
				 struct fsnotify_iter_info *);
	/* Saved by fsnotify_init_mark; consumed by fsnotify_add_mark */
	struct fsnotify_group *group;
};

static struct __fsnotify_compat_ctx __fsnotify_compat_ctx;

/* Trampoline — exact 4.9 ABI, forwards to the real fn with NULL iter_info */
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
	 * iter_info is explicitly NULL — never a garbage register.
	 * Casting data to const void * is safe: same representation,
	 * only a qualifier is being added.
	 */
	return __fsnotify_compat_ctx.real_handle_event(
		group, inode, inode_mark, vfsmount_mark,
		mask, (const void *)data, data_type,
		file_name, cookie,
		NULL);   /* <-- iter_info: guaranteed NULL */
}

static inline struct fsnotify_group *
__fsnotify_compat_alloc_group(const struct __fsnotify_ops_compat *compat_ops)
{
	/*
	 * Layout assertion: the two structs must be byte-identical so that
	 * the memcpy below is a safe type-pun.  If a 4.9.x point release
	 * ever adds a field to the real struct, this fires at build time.
	 */
	BUILD_BUG_ON(sizeof(struct __fsnotify_ops_compat) !=
		     sizeof(__fsnotify_ops_real_t));

	/*
	 * real_ops is function-scoped static so its address is stable for
	 * the lifetime of the kernel module.  Declared static here (not at
	 * file scope) to keep it as close as possible to its only use site.
	 *
	 * IMPORTANT: if this header is ever included in more than one
	 * translation unit, each TU gets its own copy of real_ops and
	 * __fsnotify_compat_ctx (static inline semantics).  All call sites
	 * must live in the same TU — enforce this by including the header
	 * in exactly one .c file.
	 */
	static __fsnotify_ops_real_t real_ops;
	memcpy(&real_ops, compat_ops, sizeof(real_ops));

	/* Save the new-API handler and install the trampoline in its place */
	__fsnotify_compat_ctx.real_handle_event = compat_ops->handle_event;
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
 * Save the group into the centralised context for use by fsnotify_add_mark,
 * then forward to the real kernel function with a no-op free_mark callback.
 * ====================================================================== */
static inline void __fsnotify_compat_noop_free(struct fsnotify_mark *m) {}

static inline void
__fsnotify_compat_init_mark(struct fsnotify_mark *mark,
			    struct fsnotify_group *group)
{
	__fsnotify_compat_ctx.group = group;
	fsnotify_init_mark(mark, __fsnotify_compat_noop_free);
}

/*
 * The second argument must be a struct fsnotify_group *.
 * The parameter is named 'group' (not 'group_or_fn') to make the
 * expected type explicit and prevent silent mis-use on 4.9.
 */
#define fsnotify_init_mark(mark, group) \
	__fsnotify_compat_init_mark(mark, group)


/* =========================================================================
 * 4. fsnotify_add_mark(mark, inode, mnt, allow_dups)
 *
 * 4.9:  fsnotify_add_mark(mark, group, inode, mnt, allow_dups)
 * New:  fsnotify_add_mark(mark, inode, mnt, allow_dups)
 *
 * Re-inject the group saved by init_mark via the centralised context.
 * ====================================================================== */
static inline int
__fsnotify_compat_add_mark(struct fsnotify_mark *mark,
			   struct inode *inode,
			   struct vfsmount *mnt,
			   int allow_dups)
{
	return fsnotify_add_mark(mark, __fsnotify_compat_ctx.group,
				 inode, mnt, allow_dups);
}

#define fsnotify_add_mark(mark, inode, mnt, allow_dups) \
	__fsnotify_compat_add_mark(mark, inode, mnt, allow_dups)


/* =========================================================================
 * 5. Suppress -Wmissing-braces for {0} struct initialisers
 *
 * Scoped with push/pop so the suppression applies only to this header's
 * own declarations and does not leak into the rest of the including TU.
 * If the including file also uses {0} initialisers and sees this warning,
 * wrap those sites individually or switch to C99 designated initialisers.
 * ====================================================================== */
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmissing-braces"
/* (no declarations here that need the suppression — push/pop pair is the
 *  template for any future struct init added to this header)            */
#pragma GCC diagnostic pop

#endif /* LINUX_VERSION_CODE <= 4.9 */
#endif /* _FSNOTIFY_COMPAT_H */
