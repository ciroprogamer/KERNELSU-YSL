#!/usr/bin/env python3
"""
strip_kconfig.py — Remove any CONFIG_* option from kernel sources,
                   Makefiles, and Kconfig files.

The tool treats the target config as *not defined* everywhere, so:

C/H source behaviour:
  #ifdef  CONFIG_FOO*  …          #endif  → entire block deleted
  #ifndef CONFIG_FOO*  …          #endif  → wrapper removed, content kept
  #ifdef  CONFIG_FOO*  … #else …  #endif  → only #else branch kept
  #ifndef CONFIG_FOO*  … #else …  #endif  → only #ifdef branch kept
  #if defined(CONFIG_FOO*)        …       → same rules as #ifdef
  #elif   CONFIG_FOO*  …                  → elif clause dropped

Makefile behaviour:
  Any line whose non-comment content references CONFIG_FOO* is dropped.
  Handles obj-$(CONFIG_FOO*), ccflags-y += ..., ifeq/ifneq blocks, etc.

Kconfig behaviour:
  Each 'config FOO*' stanza is dropped entirely.
  'if CONFIG_FOO* … endif' blocks at menu level are also dropped.

Nested blocks are handled correctly.

Usage (non-interactive):
    # Dry-run — default; prints what would change and writes a patch:
    python3 strip_kconfig.py --config CONFIG_KSU_SUSFS /path/to/kernel
    python3 strip_kconfig.py --config CONFIG_KSU_SUSFS --diff-out my.patch /path/to/kernel

    # Actually modify files:
    python3 strip_kconfig.py --config CONFIG_KSU_SUSFS --direct-run /path/to/kernel

    # Process a single file:
    python3 strip_kconfig.py --config CONFIG_MY_FEATURE fs/exec.c

    # Exclude directories:
    python3 strip_kconfig.py --config CONFIG_MY_FEATURE /path/to/kernel --exclude drivers/staging

    # Custom C/H extensions (Makefile/Kconfig always processed when walking):
    python3 strip_kconfig.py --config CONFIG_MY_FEATURE --ext .c .h .cpp /path/to/kernel

Interactive usage (no arguments):
    python3 strip_kconfig.py
"""

import argparse
import difflib
import os
import re
import sys
from pathlib import Path
from typing import List, Tuple, Optional


# ---------------------------------------------------------------------------
# Config pattern matcher — all regex patterns derived from one prefix
# ---------------------------------------------------------------------------

class ConfigMatcher:
    """Holds all compiled patterns for a given CONFIG_* prefix."""

    def __init__(self, config_option: str):
        # Normalise: accept both 'MY_FEATURE' and 'CONFIG_MY_FEATURE'
        if not config_option.startswith('CONFIG_'):
            config_option = 'CONFIG_' + config_option
        self.option = config_option
        esc = re.escape(config_option)

        # General "does this text mention our option?"
        self._re = re.compile(r'\b' + esc)

        # Makefile: ifeq/ifneq whose condition contains our option
        self.make_if_re = re.compile(
            r'^[ \t]*(?:ifeq|ifneq)\s*\([^)]*' + esc + r'[^)]*\)'
        )
        # Makefile: ifdef/ifndef whose variable is our option
        self.make_ifdef_re = re.compile(
            r'^[ \t]*(?:ifdef|ifndef)\s+' + esc
        )

        # For _parse_if_expr
        self._esc = esc

    def search(self, text: str) -> bool:
        """True if text contains our CONFIG option."""
        return bool(self._re.search(text))

    def parse_if_expr(self, expr: str) -> Tuple[bool, bool]:
        """Return (is_our_option, is_negated) for a #if expression."""
        expr = expr.strip()
        esc = self._esc
        if re.match(r'^!\s*\(?\s*defined\s*\(\s*' + esc + r'\w*\s*\)', expr):
            return True, True
        if re.match(r'^defined\s*\(\s*' + esc + r'\w*\s*\)', expr):
            return True, False
        if self._re.match(expr):
            return True, False
        if self.search(expr):
            return True, False
        return False, False


# ---------------------------------------------------------------------------
# C / H source processor
# ---------------------------------------------------------------------------

_DIRECTIVE_RE = re.compile(
    r'^(?P<indent>[ \t]*)#[ \t]*'
    r'(?P<directive>ifdef|ifndef|if|elif|else|endif)'
    r'(?P<rest>[^\n]*)',
    re.MULTILINE,
)


def process_c(source: str, cm: ConfigMatcher) -> Tuple[str, int]:
    """Strip target config preprocessor blocks from C/H source."""
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
                matched = cm.search(rest)
                negated = False
            elif directive == 'ifndef':
                matched = cm.search(rest)
                negated = True
            else:
                matched, negated = cm.parse_if_expr(rest)

            parent_suppress = stack[-1]['suppress'] if stack else False

            if matched and not parent_suppress:
                suppress_block = not negated
                stack.append({
                    'directive': directive,
                    'matched': True,
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
                    'matched': False,
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
            parent_suppressed = (stack[-2]['suppress'] if len(stack) > 1 else False)
            if top['matched'] and not parent_suppressed:
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
            if top['directive'] not in ('ifdef', 'ifndef'):
                matched, negated = cm.parse_if_expr(rest)
            else:
                matched, negated = False, False
            if top['matched']:
                result[i] = None
                edits += 1
                top['suppress'] = (not negated) if matched else True
            else:
                if top['suppress']:
                    result[i] = None
                    edits += 1

        elif directive == 'endif':
            if not stack:
                i += 1
                continue
            top = stack.pop()
            if top['matched']:
                result[i] = None
                edits += 1
            else:
                if top['suppress']:
                    result[i] = None
                    edits += 1

        i += 1

    new_source = ''.join(l for l in result if l is not None)
    return new_source, edits


# ---------------------------------------------------------------------------
# Makefile processor
# ---------------------------------------------------------------------------

_MAKE_ANY_IF_RE = re.compile(r'^[ \t]*(?:ifeq|ifneq|ifdef|ifndef)\b')
_MAKE_ELSE_RE   = re.compile(r'^[ \t]*else\b')
_MAKE_ENDIF_RE  = re.compile(r'^[ \t]*endif\b')


def process_makefile(source: str, cm: ConfigMatcher) -> Tuple[str, int]:
    """
    Strip config references from a Makefile.

    Rules:
      1. ifeq/ifneq/ifdef/ifndef blocks whose condition references our option
         → entire block (including else branch) dropped.
      2. Plain lines that reference our option → dropped, including
         any line-continuation backslash lines.
    """
    lines = source.splitlines(keepends=True)
    result: List[Optional[str]] = list(lines)
    edits = 0

    stack: List[dict] = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.rstrip('\n\r')
        in_suppress = stack[-1]['suppress'] if stack else False

        # SUSFS/config conditional opener
        if cm.make_if_re.match(stripped) or cm.make_ifdef_re.match(stripped):
            stack.append({'matched': True, 'suppress': True, 'nested': 0})
            result[i] = None
            edits += 1
            i += 1
            continue

        # Any other conditional opener
        if _MAKE_ANY_IF_RE.match(stripped):
            if in_suppress:
                stack[-1]['nested'] += 1
                result[i] = None
                edits += 1
            else:
                stack.append({'matched': False, 'suppress': False, 'nested': 0})
            i += 1
            continue

        # else
        if _MAKE_ELSE_RE.match(stripped):
            if stack and stack[-1]['matched'] and stack[-1]['nested'] == 0:
                stack[-1]['suppress'] = not stack[-1]['suppress']
                result[i] = None
                edits += 1
            elif in_suppress:
                result[i] = None
                edits += 1
            i += 1
            continue

        # endif
        if _MAKE_ENDIF_RE.match(stripped):
            if stack:
                top = stack[-1]
                if top['nested'] > 0:
                    top['nested'] -= 1
                    if in_suppress:
                        result[i] = None
                        edits += 1
                else:
                    if top['matched'] or in_suppress:
                        result[i] = None
                        edits += 1
                    stack.pop()
            i += 1
            continue

        # Inside a suppressed block
        if in_suppress:
            result[i] = None
            edits += 1
            i += 1
            continue

        # Plain line referencing our config option
        code_part = re.sub(r'(?<![\\])#.*', '', stripped)
        if cm.search(code_part):
            result[i] = None
            edits += 1
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

_KCONFIG_STANZA_RE = re.compile(
    r'^(?:config|menuconfig|choice|endchoice|comment|menu|endmenu|source|mainmenu)\b'
)
_KCONFIG_IF_RE = re.compile(r'^if\s+(.+)')
_KCONFIG_ENDIF = re.compile(r'^endif\b')


def process_kconfig(source: str, cm: ConfigMatcher) -> Tuple[str, int]:
    """
    Remove every matching 'config FOO*' stanza and 'if CONFIG_FOO* … endif'
    block from a Kconfig file.
    """
    lines = source.splitlines(keepends=True)
    result: List[Optional[str]] = list(lines)
    edits = 0

    if_stack: List[bool] = []
    suppress_stanza = False

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        in_matched_if = bool(if_stack) and any(if_stack)

        # Top-level 'if EXPR'
        m_if = _KCONFIG_IF_RE.match(stripped)
        if m_if and not suppress_stanza:
            cond = m_if.group(1).strip()
            is_match = cm.search(cond)
            if_stack.append(is_match)
            if is_match or in_matched_if:
                result[i] = None
                edits += 1
            i += 1
            continue

        # 'endif'
        if _KCONFIG_ENDIF.match(stripped) and not suppress_stanza:
            was_match = if_stack.pop() if if_stack else False
            new_in_match = bool(if_stack) and any(if_stack)
            if was_match or new_in_match:
                result[i] = None
                edits += 1
            i += 1
            continue

        # Top-level stanza opener
        if _KCONFIG_STANZA_RE.match(stripped):
            suppress_stanza = False  # previous stanza is over

            m_cfg = re.match(r'^(?:config|menuconfig)\s+(\w+)', stripped)
            if m_cfg and cm.search('CONFIG_' + m_cfg.group(1)):
                suppress_stanza = True

            if suppress_stanza or in_matched_if:
                result[i] = None
                edits += 1
            i += 1
            continue

        # Inside a suppressed stanza or if-block
        if suppress_stanza or in_matched_if:
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


def _process_source(path: Path, kind: str, cm: ConfigMatcher) -> Tuple[str, str, int]:
    original = path.read_text(encoding='utf-8', errors='replace')
    if kind == 'makefile':
        new_source, edits = process_makefile(original, cm)
    elif kind == 'kconfig':
        new_source, edits = process_kconfig(original, cm)
    else:
        new_source, edits = process_c(original, cm)
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
# File processing
# ---------------------------------------------------------------------------

DEFAULT_EXTENSIONS = {'.c', '.h', '.cpp', '.cc', '.cxx', '.S', '.asm'}


def process_file(
    path: Path,
    dry_run: bool,
    cm: ConfigMatcher,
    diff_chunks: Optional[List[str]] = None,
    kind: str = 'c',
) -> bool:
    """Process a single file. Returns True if changed (or would be)."""
    try:
        original, new_source, edits = _process_source(path, kind, cm)
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
    cm: ConfigMatcher,
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
            if process_file(fpath, dry_run, cm, diff_chunks, kind):
                changed += 1

    return changed, scanned


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def _prompt(msg: str, default: str = '') -> str:
    """Prompt the user for input, showing a default value."""
    if default:
        answer = input(f'{msg} [{default}]: ').strip()
        return answer if answer else default
    else:
        while True:
            answer = input(f'{msg}: ').strip()
            if answer:
                return answer
            print('  (required — please enter a value)')


def _prompt_yn(msg: str, default: bool = True) -> bool:
    hint = 'Y/n' if default else 'y/N'
    answer = input(f'{msg} [{hint}]: ').strip().lower()
    if not answer:
        return default
    return answer in ('y', 'yes')


def interactive_mode() -> argparse.Namespace:
    """Walk the user through all options interactively."""
    print('=' * 60)
    print('  strip_kconfig — interactive mode')
    print('  (Run with --help to see non-interactive usage.)')
    print('=' * 60)
    print()

    # --- config option ---
    print('Which CONFIG_* option do you want to remove?')
    print('  Examples: CONFIG_KSU_SUSFS  |  CONFIG_MY_DRIVER  |  MY_FEATURE')
    raw_option = _prompt('CONFIG option')
    if not raw_option.startswith('CONFIG_'):
        raw_option = 'CONFIG_' + raw_option

    # --- paths ---
    print()
    print('Path(s) to process — a kernel root directory or a single file.')
    print('  Separate multiple paths with spaces.')
    raw_paths = _prompt('Path(s)')
    paths = raw_paths.split()

    # --- extensions ---
    print()
    default_ext_str = ' '.join(sorted(DEFAULT_EXTENSIONS))
    print(f'C/H file extensions to scan (Makefile/Kconfig always included).')
    raw_ext = _prompt('Extensions', default_ext_str)
    extensions = {e.strip() if e.strip().startswith('.') else '.' + e.strip()
                  for e in raw_ext.split()}

    # --- excludes ---
    print()
    raw_excl = input('Directories to exclude (space-separated, or Enter to skip): ').strip()
    excludes = set(raw_excl.split()) if raw_excl else set()

    # --- dry-run vs direct-run ---
    print()
    dry_run = _prompt_yn('Dry run only? (no files will be modified)', default=True)

    # --- diff output ---
    diff_out = 'strip_kconfig.patch'
    if dry_run:
        print()
        diff_out = _prompt('Save diff/patch to', diff_out)

    print()
    print('-' * 60)
    print(f'  Option  : {raw_option}')
    print(f'  Path(s) : {", ".join(paths)}')
    print(f'  Ext     : {" ".join(sorted(extensions))}')
    if excludes:
        print(f'  Exclude : {", ".join(sorted(excludes))}')
    print(f'  Mode    : {"DRY RUN (no files changed)" if dry_run else "DIRECT RUN (files WILL be modified)"}')
    if dry_run:
        print(f'  Diff out: {diff_out}')
    print('-' * 60)
    print()

    if not dry_run:
        if not _prompt_yn('Files will be modified. Proceed?', default=False):
            print('Aborted.')
            sys.exit(0)

    ns = argparse.Namespace(
        config=raw_option,
        paths=paths,
        ext=list(extensions),
        exclude=list(excludes),
        dry_run=dry_run,
        diff_out=diff_out,
        direct_run=not dry_run,
    )
    return ns


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Remove any CONFIG_* option from kernel sources, Makefiles, and Kconfig files.\n\n'
            'Run without arguments for interactive mode.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--config', metavar='OPTION', default=None,
        help='CONFIG_* option to remove, e.g. CONFIG_KSU_SUSFS or just KSU_SUSFS. (required in non-interactive mode)')
    parser.add_argument(
        'paths', nargs='*', metavar='PATH',
        help='File(s) or directory to process.')
    parser.add_argument(
        '--direct-run', action='store_true',
        help='Actually modify files in place. Without this flag the tool runs in '
             'dry-run mode (default): it prints what would change and writes a patch.')
    parser.add_argument(
        '--diff-out', metavar='FILE', default='strip_kconfig.patch',
        help='(dry-run only) Path for the unified-diff patch file. '
             'Default: strip_kconfig.patch')
    parser.add_argument(
        '--ext', nargs='+', metavar='EXT',
        default=list(DEFAULT_EXTENSIONS),
        help='C/H file extensions to process '
             '(default: .c .h .cpp .cc .cxx .S .asm). '
             'Makefile and Kconfig files are always processed when walking.')
    parser.add_argument(
        '--exclude', nargs='+', metavar='DIR', default=[],
        help='Directories to skip (relative to each root or absolute).')
    return parser


def main():
    # Interactive mode: no arguments at all
    if len(sys.argv) == 1:
        args = interactive_mode()
    else:
        parser = build_parser()
        args = parser.parse_args()
        args.dry_run = not args.direct_run

        if not args.config:
            parser.error('--config OPTION is required in non-interactive mode.')
        if not args.paths:
            parser.error('at least one PATH is required in non-interactive mode.')

    cm = ConfigMatcher(args.config)
    extensions = {e if e.startswith('.') else '.' + e for e in args.ext}
    excludes = set(args.exclude)

    print(f'Targeting : {cm.option}')
    print(f'Mode      : {"DRY RUN" if args.dry_run else "DIRECT RUN"}')
    if excludes:
        print(f'Excluding : {", ".join(sorted(excludes))}')
    print()

    diff_chunks: Optional[List[str]] = [] if args.dry_run else None

    total_changed = total_scanned = 0

    for raw_path in args.paths:
        p = Path(raw_path)
        if not p.exists():
            print(f'[ERROR] Path not found: {p}', file=sys.stderr)
            continue
        if p.is_file():
            total_scanned += 1
            kind = 'c' if p.suffix in extensions else _classify(p)
            if process_file(p, args.dry_run, cm, diff_chunks, kind):
                total_changed += 1
        elif p.is_dir():
            changed, scanned = walk(p, extensions, args.dry_run, cm, excludes, diff_chunks)
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
                line_count = sum(c.count('\n') for c in diff_chunks)
                print(f'Diff written to: {diff_path}  ({line_count} line(s))')
            except Exception as e:
                print(f'[ERROR] Could not write diff file: {e}', file=sys.stderr)
        else:
            print('No changes detected — diff file not written.')


if __name__ == '__main__':
    main()
