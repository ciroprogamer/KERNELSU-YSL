#!/usr/bin/env python3
"""
strip_susfs.py — Remove all CONFIG_KSU_SUSFS references from kernel sources,
                 Makefiles, and Kconfig files.

C/H source behaviour (as if CONFIG_KSU_SUSFS is *not* defined):
  #ifdef  CONFIG_KSU_SUSFS*  …          #endif  → entire block deleted
  #ifndef CONFIG_KSU_SUSFS*  …          #endif  → wrapper removed, content kept
  #ifdef  CONFIG_KSU_SUSFS*  … #else …  #endif  → only #else branch kept
  #ifndef CONFIG_KSU_SUSFS*  … #else …  #endif  → only #ifdef branch kept
  #if defined(CONFIG_KSU_SUSFS*)         …       → same rules as #ifdef
  #elif   CONFIG_KSU_SUSFS*  …                   → elif clause dropped

Makefile behaviour:
  Any line whose non-comment content references CONFIG_KSU_SUSFS* is dropped.
  Handles obj-$(CONFIG_KSU_SUSFS*), ccflags-y += ..., ifeq/ifneq blocks, etc.

Kconfig behaviour:
  Each 'config KSU_SUSFS*' stanza (from the config line to just before the next
  top-level keyword or end-of-file) is dropped entirely.
  'if CONFIG_KSU_SUSFS* … endif' blocks at menu level are also dropped.

Nested blocks are handled correctly.

Usage:
    # Dry-run (print what would change, write a unified diff):
    python3 strip_susfs.py --dry-run /path/to/kernel
    python3 strip_susfs.py --dry-run --diff-out changes.patch /path/to/kernel

    # Process all files in-place:
    python3 strip_susfs.py /path/to/kernel

    # Process a single file:
    python3 strip_susfs.py fs/exec.c
    python3 strip_susfs.py fs/Makefile
    python3 strip_susfs.py fs/Kconfig

    # Exclude specific directories:
    python3 strip_susfs.py /path/to/kernel --exclude drivers/staging tools out

    # Custom C/H extension list (Makefile/Kconfig always processed when walking):
    python3 strip_susfs.py --ext .c .h .cpp /path/to/kernel
"""

import argparse
import difflib
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple, Optional

# ---------------------------------------------------------------------------
# Shared pattern
# ---------------------------------------------------------------------------

_SUSFS_RE = re.compile(r'\bCONFIG_KSU_SUSFS')


def _is_susfs(expr: str) -> bool:
    return bool(_SUSFS_RE.search(expr))


# ---------------------------------------------------------------------------
# C / H source processor
# ---------------------------------------------------------------------------

_DIRECTIVE_RE = re.compile(
    r'^(?P<indent>[ \t]*)#[ \t]*'
    r'(?P<directive>ifdef|ifndef|if|elif|else|endif)'
    r'(?P<rest>[^\n]*)',
    re.MULTILINE,
)


def process_c(source: str) -> Tuple[str, int]:
    """Strip SUSFS preprocessor blocks from C/H source. Returns (new_source, edits)."""
    lines = source.splitlines(keepends=True)
    result: List[Optional[str]] = list(lines)
    edits = 0
    stack = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = _DIRECTIVE_RE.match(line)
        if not m:
            if stack and stack[-1]['suppress']:
                result[i] = None
                edits += 1
            i += 1
            continue

        directive = m.group('directive')
        rest = m.group('rest').strip()

        if directive in ('ifdef', 'ifndef', 'if'):
            if directive == 'ifdef':
                susfs = _is_susfs(rest)
                negated = False
            elif directive == 'ifndef':
                susfs = _is_susfs(rest)
                negated = True
            else:
                susfs, negated = _parse_if_expr(rest)

            parent_suppress = stack[-1]['suppress'] if stack else False

            if susfs and not parent_suppress:
                suppress_block = not negated
                stack.append({
                    'directive': directive,
                    'susfs': True,
                    'negated': negated,
                    'suppress': suppress_block,
                    'in_else': False,
                    'line_idx': i,
                })
                result[i] = None
                edits += 1
            else:
                suppress = parent_suppress
                stack.append({
                    'directive': directive,
                    'susfs': False,
                    'negated': False,
                    'suppress': suppress,
                    'in_else': False,
                    'line_idx': i,
                })
                if suppress:
                    result[i] = None
                    edits += 1

        elif directive == 'else':
            if not stack:
                i += 1
                continue
            top = stack[-1]
            if top['susfs'] and not (stack[:-1] and stack[-2]['suppress'] if len(stack) > 1 else False):
                top['in_else'] = True
                top['suppress'] = not top['suppress']
                result[i] = None
                edits += 1
            else:
                if top['suppress']:
                    result[i] = None
                    edits += 1

        elif directive == 'elif':
            if not stack:
                i += 1
                continue
            top = stack[-1]
            susfs, negated = _parse_if_expr(rest) if top['directive'] not in ('ifdef', 'ifndef') else (False, False)
            if top['susfs']:
                result[i] = None
                edits += 1
                top['suppress'] = (not negated) if susfs else True
            else:
                if top['suppress']:
                    result[i] = None
                    edits += 1

        elif directive == 'endif':
            if not stack:
                i += 1
                continue
            top = stack.pop()
            if top['susfs']:
                result[i] = None
                edits += 1
            else:
                if top['suppress']:
                    result[i] = None
                    edits += 1

        i += 1

    new_source = ''.join(l for l in result if l is not None)
    return new_source, edits


def _parse_if_expr(expr: str) -> Tuple[bool, bool]:
    """Return (is_susfs, is_negated) for a #if expression."""
    expr = expr.strip()
    if re.match(r'^!\s*\(?\s*defined\s*\(\s*CONFIG_KSU_SUSFS\w*\s*\)', expr):
        return True, True
    if re.match(r'^defined\s*\(\s*CONFIG_KSU_SUSFS\w*\s*\)', expr):
        return True, False
    if _SUSFS_RE.match(expr):
        return True, False
    if _is_susfs(expr):
        return True, False
    return False, False


# ---------------------------------------------------------------------------
# Makefile processor
# ---------------------------------------------------------------------------

# ifeq/ifneq whose condition mentions a SUSFS config variable.
_MAKE_SUSFS_IF_RE = re.compile(
    r'^[ \t]*(?:ifeq|ifneq)\s*\([^)]*CONFIG_KSU_SUSFS[^)]*\)'
)
# ifdef/ifndef whose variable name is a SUSFS config.
_MAKE_SUSFS_IFDEF_RE = re.compile(
    r'^[ \t]*(?:ifdef|ifndef)\s+CONFIG_KSU_SUSFS'
)
# Any make conditional (to track nesting depth inside suppressed blocks).
_MAKE_ANY_IF_RE = re.compile(r'^[ \t]*(?:ifeq|ifneq|ifdef|ifndef)\b')
_MAKE_ELSE_RE   = re.compile(r'^[ \t]*else\b')
_MAKE_ENDIF_RE  = re.compile(r'^[ \t]*endif\b')


def process_makefile(source: str) -> Tuple[str, int]:
    """
    Strip SUSFS references from a Makefile.

    Rules:
      1. ifeq/ifneq/ifdef/ifndef blocks whose condition references
         CONFIG_KSU_SUSFS* → entire block (including else branch) dropped.
      2. Plain lines (obj-y, ccflags-y, etc.) that reference CONFIG_KSU_SUSFS*
         → dropped, along with any line-continuation backslash lines.
    """
    lines = source.splitlines(keepends=True)
    result: List[Optional[str]] = list(lines)
    edits = 0

    # Stack: {'susfs': bool, 'suppress': bool, 'nested': int}
    # 'nested' counts make-ifs opened *inside* the current frame so we know
    # which endif closes this frame vs a deeper one.
    stack: List[dict] = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip('\n\r')
        in_suppress = stack[-1]['suppress'] if stack else False

        # --- SUSFS conditional opener ---
        if _MAKE_SUSFS_IF_RE.match(stripped) or _MAKE_SUSFS_IFDEF_RE.match(stripped):
            stack.append({'susfs': True, 'suppress': True, 'nested': 0})
            result[i] = None
            edits += 1
            i += 1
            continue

        # --- any other conditional opener ---
        if _MAKE_ANY_IF_RE.match(stripped):
            if in_suppress:
                stack[-1]['nested'] += 1
                result[i] = None
                edits += 1
            else:
                stack.append({'susfs': False, 'suppress': False, 'nested': 0})
            i += 1
            continue

        # --- else ---
        if _MAKE_ELSE_RE.match(stripped):
            if stack and stack[-1]['susfs'] and stack[-1]['nested'] == 0:
                # Flip suppression for the else branch of a susfs block.
                stack[-1]['suppress'] = not stack[-1]['suppress']
                result[i] = None
                edits += 1
            elif in_suppress:
                result[i] = None
                edits += 1
            i += 1
            continue

        # --- endif ---
        if _MAKE_ENDIF_RE.match(stripped):
            if stack:
                top = stack[-1]
                if top['nested'] > 0:
                    top['nested'] -= 1
                    if in_suppress:
                        result[i] = None
                        edits += 1
                else:
                    if top['susfs'] or in_suppress:
                        result[i] = None
                        edits += 1
                    stack.pop()
            i += 1
            continue

        # --- inside a suppressed block ---
        if in_suppress:
            result[i] = None
            edits += 1
            i += 1
            continue

        # --- plain line referencing SUSFS ---
        # Strip make-style comment before checking.
        code_part = re.sub(r'(?<![\\])#.*', '', stripped)
        if _is_susfs(code_part):
            result[i] = None
            edits += 1
            # Consume continuation lines.
            while stripped.endswith('\\'):
                i += 1
                if i >= len(lines):
                    break
                stripped = lines[i].rstrip('\n\r')
                result[i] = None
                edits += 1
            i += 1
            continue

        i += 1

    new_source = ''.join(l for l in result if l is not None)
    return new_source, edits


# ---------------------------------------------------------------------------
# Kconfig processor
# ---------------------------------------------------------------------------

# Keywords that start a new top-level Kconfig stanza.
_KCONFIG_STANZA_RE = re.compile(
    r'^(?:config|menuconfig|choice|endchoice|comment|menu|endmenu|source|mainmenu)\b'
)
# 'if EXPR' at top level (not inside a config stanza's depends/select lines).
_KCONFIG_IF_RE  = re.compile(r'^if\s+(.+)')
_KCONFIG_ENDIF  = re.compile(r'^endif\b')


def process_kconfig(source: str) -> Tuple[str, int]:
    """
    Remove every 'config KSU_SUSFS*' stanza and 'if CONFIG_KSU_SUSFS* … endif'
    block from a Kconfig file.

    A config stanza runs from its 'config ...' opener to just before the next
    top-level keyword (or EOF).  Indented continuation lines belong to the
    current stanza.
    """
    lines = source.splitlines(keepends=True)
    result: List[Optional[str]] = list(lines)
    edits = 0

    # Stack of booleans: True = this 'if' block is a SUSFS if → suppress.
    if_stack: List[bool] = []
    suppress_stanza = False  # inside a dropped 'config KSU_SUSFS*' stanza

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        in_susfs_if = bool(if_stack) and any(if_stack)

        # --- top-level 'if EXPR' ---
        m_if = _KCONFIG_IF_RE.match(stripped)
        if m_if and not suppress_stanza:
            cond = m_if.group(1).strip()
            is_sus = _is_susfs(cond)
            if_stack.append(is_sus)
            if is_sus or in_susfs_if:
                result[i] = None
                edits += 1
            i += 1
            continue

        # --- 'endif' ---
        if _KCONFIG_ENDIF.match(stripped) and not suppress_stanza:
            was_sus = if_stack.pop() if if_stack else False
            new_in_susfs = bool(if_stack) and any(if_stack)
            if was_sus or new_in_susfs:
                result[i] = None
                edits += 1
            i += 1
            continue

        # --- top-level stanza opener ---
        if _KCONFIG_STANZA_RE.match(stripped):
            suppress_stanza = False  # reset: previous stanza is over

            m_cfg = re.match(r'^(?:config|menuconfig)\s+(\w+)', stripped)
            if m_cfg and _is_susfs('CONFIG_' + m_cfg.group(1)):
                suppress_stanza = True

            if suppress_stanza or in_susfs_if:
                result[i] = None
                edits += 1
            i += 1
            continue

        # --- inside a suppressed stanza or if-block ---
        if suppress_stanza or in_susfs_if:
            result[i] = None
            edits += 1
            i += 1
            continue

        i += 1

    new_source = ''.join(l for l in result if l is not None)
    return new_source, edits


# ---------------------------------------------------------------------------
# File type dispatch
# ---------------------------------------------------------------------------

def _classify(path: Path) -> str:
    """Return 'c', 'makefile', or 'kconfig'."""
    name = path.name
    suffix = path.suffix

    if name == 'Kconfig' or name.startswith('Kconfig.'):
        return 'kconfig'
    if name in ('Makefile', 'makefile', 'GNUmakefile') or suffix == '.mk':
        return 'makefile'
    return 'c'


def _process_source(path: Path, kind: str) -> Tuple[str, str, int]:
    original = path.read_text(encoding='utf-8', errors='replace')
    if kind == 'makefile':
        new_source, edits = process_makefile(original)
    elif kind == 'kconfig':
        new_source, edits = process_kconfig(original)
    else:
        new_source, edits = process_c(original)
    return original, new_source, edits


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def make_unified_diff(original: str, new_source: str, filepath: Path) -> str:
    """Return a unified diff string for a single file, or '' if no changes."""
    label = str(filepath)
    diff_lines = list(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f'a/{label}',
        tofile=f'b/{label}',
    ))
    return ''.join(diff_lines)


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS = {'.c', '.h', '.cpp', '.cc', '.cxx', '.S', '.asm'}


def process_file(
    path: Path,
    dry_run: bool,
    diff_chunks: Optional[List[str]] = None,
    kind: str = 'c',
) -> bool:
    """Process a single file.  Returns True if changed (or would be)."""
    try:
        original, new_source, edits = _process_source(path, kind)
    except Exception as e:
        print(f'  [SKIP] {path}: cannot read — {e}', file=sys.stderr)
        return False

    if edits == 0:
        return False

    tag = {'c': 'C/H ', 'makefile': 'MAKE', 'kconfig': 'KCFG'}[kind]
    if dry_run:
        print(f'  [DRY/{tag}]  {path}  ({edits} edit(s))')
        if diff_chunks is not None:
            chunk = make_unified_diff(original, new_source, path)
            if chunk:
                diff_chunks.append(chunk)
    else:
        path.write_text(new_source, encoding='utf-8')
        print(f'  [DONE/{tag}] {path}  ({edits} edit(s))')
    return True


def walk(
    root: Path,
    extensions: set,
    dry_run: bool,
    exclude: set = None,
    diff_chunks: Optional[List[str]] = None,
) -> Tuple[int, int]:
    """Walk a directory tree. Returns (files_changed, files_scanned)."""
    exclude = exclude or set()

    resolved_excludes = set()
    for e in exclude:
        p = Path(e)
        resolved_excludes.add(p.resolve() if p.is_absolute() else (root / p).resolve())

    changed = 0
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith('.')
            and d not in ('out', 'build', '__pycache__')
            and (current / d).resolve() not in resolved_excludes
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            p = Path(fname)
            suffix = p.suffix

            if p.name in ('Makefile', 'makefile', 'GNUmakefile') or suffix == '.mk':
                kind = 'makefile'
            elif p.name == 'Kconfig' or p.name.startswith('Kconfig.'):
                kind = 'kconfig'
            elif suffix in extensions:
                kind = 'c'
            else:
                continue

            scanned += 1
            if process_file(fpath, dry_run, diff_chunks, kind):
                changed += 1

    return changed, scanned


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Remove CONFIG_KSU_SUSFS references from kernel sources, '
                    'Makefiles, and Kconfig files.')
    parser.add_argument(
        'paths', nargs='+', metavar='PATH',
        help='File(s) or directory to process.')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print what would change without modifying any file.')
    parser.add_argument(
        '--diff-out', metavar='FILE', default='susfs_strip.patch',
        help='(dry-run only) Path for the unified-diff output file. '
             'Default: susfs_strip.patch')
    parser.add_argument(
        '--ext', nargs='+', metavar='EXT',
        default=list(DEFAULT_EXTENSIONS),
        help='C/H file extensions to process '
             '(default: .c .h .cpp .cc .cxx .S .asm). '
             'Makefile and Kconfig files are always processed when walking.')
    parser.add_argument(
        '--exclude', nargs='+', metavar='DIR', default=[],
        help='Directories to skip (relative to each root or absolute).')
    args = parser.parse_args()

    extensions = {e if e.startswith('.') else '.' + e for e in args.ext}
    excludes = set(args.exclude)

    if excludes:
        print(f'Excluding: {", ".join(sorted(excludes))}')

    diff_chunks: Optional[List[str]] = [] if args.dry_run else None

    total_changed = total_scanned = 0

    for raw_path in args.paths:
        p = Path(raw_path)
        if not p.exists():
            print(f'[ERROR] Path not found: {p}', file=sys.stderr)
            continue
        if p.is_file():
            total_scanned += 1
            # Explicit single-file: infer kind by name, fall back to 'c'.
            if p.suffix in extensions:
                kind = 'c'
            else:
                kind = _classify(p)
            if process_file(p, args.dry_run, diff_chunks, kind):
                total_changed += 1
        elif p.is_dir():
            changed, scanned = walk(p, extensions, args.dry_run, excludes, diff_chunks)
            total_changed += changed
            total_scanned += scanned
        else:
            print(f'[ERROR] Not a file or directory: {p}', file=sys.stderr)

    mode = 'Would change' if args.dry_run else 'Changed'
    print(f'\n{mode} {total_changed}/{total_scanned} file(s).')

    if args.dry_run:
        if diff_chunks:
            diff_path = Path(args.diff_out)
            try:
                diff_path.write_text(''.join(diff_chunks), encoding='utf-8')
                print(f'Diff written to: {diff_path}  '
                      f'({sum(c.count(chr(10)) for c in diff_chunks)} line(s))')
            except Exception as e:
                print(f'[ERROR] Could not write diff file: {e}', file=sys.stderr)
        else:
            print('No changes detected — diff file not written.')


if __name__ == '__main__':
    main()
