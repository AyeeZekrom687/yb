#!/usr/bin/env python3
"""
Fix decompiler bugs in YBA deobfuscated Lua script.

1. Truthiness inversions: flips all bare `if [not] VAR then` / `elseif [not] VAR then`
   Does NOT touch == ~= < > <= >= comparisons (those aren't inverted by the decompiler).
2. Setlist bugs: replaces `-- setlist _rN (batch=K)` comments with actual table packing.
3. Targeted patches: fixes specific decompiler artifacts that general passes can't handle:
   a. Namecall hook varargs passthrough (decompiler drops multi-arg forwarding)
   b. Game-ready wait loop missing break (decompiler drops break from while-true)
   c. Stand farm _G.YBAItemFarm bridge (startStandFarm/stopStandFarm registration)
4. Broken if/else: moves assignments back inside their if blocks (decompiler drops them outside).
5. Varargs passthrough: replaces register captures of ... with direct forwarding.
6. Timeout breaks: inserts missing break in `if NUMBER < _rN then end` patterns.
7. Branding: renames YBA Hub to YBA / YBA Script in user-facing strings.
8. Key stubs: replaces key-checking functions with bypass stubs.
"""

import re
import sys

# --- INVERSION FIX ---

LITERALS = {'true', 'false', 'nil'}

# Matches: if [not] IDENTIFIER then [rest]
# Group 1: indent + "if "/"elseif "
# Group 2: optional "not " (with trailing space)
# Group 3: variable name
# Group 4: " then"
# Group 5: rest of line after "then"
BARE_TRUTH_RE = re.compile(
    r'^(\s*(?:if|elseif)\s+)(not\s+)?(\w+)(\s+then\b)(.*)$'
)

def fix_inversion(line):
    """Flip a bare truthiness test if present."""
    stripped = line.rstrip('\r\n')
    m = BARE_TRUTH_RE.match(stripped)
    if not m:
        return line

    prefix, not_kw, var, then_kw, rest = m.groups()

    # Don't flip literals
    if var in LITERALS:
        return line

    # Don't flip pure numeric names (shouldn't occur, safety check)
    if var.isdigit():
        return line

    # Flip
    if not_kw:
        new_stripped = f"{prefix}{var}{then_kw}{rest}"
    else:
        new_stripped = f"{prefix}not {var}{then_kw}{rest}"

    # Preserve original line ending
    ending = line[len(stripped):]
    return new_stripped + ending


# --- SETLIST FIX ---

SETLIST_RE = re.compile(r'^(\s*)-- setlist (_\w+) \(batch=(\d+)\)\s*$')

# Extract register number from _rN
def reg_num(name):
    m = re.match(r'^_r(\d+)$', name)
    return int(m.group(1)) if m else None

def fix_setlists(lines):
    """
    Replace `-- setlist _rN (batch=K)` with table packing.

    Strategy: look backwards from the setlist comment to find the table register
    and the value registers that need packing. The SETLIST VM instruction packs
    registers _r(N+1)..._r(M) as array entries into table _rN.
    """
    result = []
    i = 0
    stats = {'fixed': 0, 'skipped': 0}

    while i < len(lines):
        stripped = lines[i].rstrip('\r\n')
        m = SETLIST_RE.match(stripped)

        if not m:
            result.append(lines[i])
            i += 1
            continue

        indent = m.group(1)
        table_reg = m.group(2)
        batch = int(m.group(3))
        tnum = reg_num(table_reg)

        if tnum is None:
            # Non-standard register name, keep as-is
            result.append(lines[i])
            stats['skipped'] += 1
            i += 1
            continue

        # Scan backwards through result to find value registers
        # Values are in registers numbered > tnum, assigned between the table
        # creation (or previous setlist) and this setlist comment.
        values = []  # (reg_number, reg_name)
        j = len(result) - 1
        found_table_creation = False

        while j >= 0:
            prev_stripped = result[j].rstrip('\r\n').strip()

            # Skip empty lines
            if not prev_stripped:
                j -= 1
                continue

            # Stop at previous setlist comment
            if prev_stripped.startswith('-- setlist'):
                break

            # Check for register assignment: [local] _rM = value
            # or: _rM = value
            assign_m = re.match(r'^(?:local\s+)?(_r(\d+))\s*=\s*(.+)$', prev_stripped)
            if assign_m:
                r_name = assign_m.group(1)
                r_num = int(assign_m.group(2))
                r_val = assign_m.group(3)

                if r_num == tnum:
                    # Found the table creation itself
                    found_table_creation = True
                    break
                elif r_num > tnum:
                    values.insert(0, (r_num, r_name, r_val))
                    j -= 1
                    continue
                else:
                    # Register before table reg, stop
                    break
            else:
                # Check for key-value assignment to the table: _rN[key] = val
                kv_m = re.match(
                    r'^' + re.escape(table_reg) + r'\[.+\]\s*=',
                    prev_stripped
                )
                if kv_m:
                    # This is a hash-part assignment, skip it
                    j -= 1
                    continue

                # Some other code - stop scanning
                break

        if not values:
            # No values found to pack - keep comment
            result.append(lines[i])
            stats['skipped'] += 1
            i += 1
            continue

        # Check for special varargs case: single value that is `...`
        if len(values) == 1 and values[0][2].strip() == '...':
            # Special case: _rN = {} ; _rM = ... ; -- setlist _rN
            # Fix: change the table creation to _rN = {...}
            # and remove the intermediate assignment
            # Find the table creation line and the varargs assignment line
            # For simplicity, replace the setlist comment with the pack
            # and modify the table creation

            # Find table creation in result
            for k in range(len(result) - 1, -1, -1):
                tk = result[k].rstrip('\r\n').strip()
                if re.match(r'^(?:local\s+)?' + re.escape(table_reg) + r'\s*=\s*\{\s*\}', tk):
                    # Replace {} with {...}
                    result[k] = result[k].replace('= {}', '= {...}', 1)
                    break

            # Remove the _rM = ... line
            for k in range(len(result) - 1, -1, -1):
                vk = result[k].rstrip('\r\n').strip()
                if re.match(r'^(?:local\s+)?_r\d+\s*=\s*\.\.\.\s*$', vk):
                    result[k] = ''  # Remove the line
                    break

            # Remove the setlist comment
            ending = lines[i][len(stripped):]
            result.append(ending if ending else '')
            stats['fixed'] += 1
            i += 1
            continue

        # General case: pack values into table
        # For batch=1: _rN = {val1, val2, ...} or _rN[1], _rN[2] = val1, val2
        # For batch>1: append at offset

        if batch == 1 and found_table_creation:
            # We can replace the table creation with an initializer
            # But only if the values are simple register references
            # For safety, use explicit index assignment
            reg_names = [v[1] for v in sorted(values, key=lambda x: x[0])]

            # Generate index assignments
            pack_lines = []
            for idx, rname in enumerate(reg_names, 1):
                pack_lines.append(f"{indent}{table_reg}[{idx}] = {rname}")

            ending = lines[i][len(stripped):]
            for pl in pack_lines:
                result.append(pl + ending)

            stats['fixed'] += 1
        elif batch > 1:
            # Multi-batch: calculate offset from previous batches
            # Each batch packs ~50 entries (Lua SETLIST batch size)
            # batch=2 starts at index 51, batch=3 at 101, etc.
            BATCH_SIZE = 50
            offset = (batch - 1) * BATCH_SIZE

            reg_names = [v[1] for v in sorted(values, key=lambda x: x[0])]
            pack_lines = []
            for idx, rname in enumerate(reg_names, offset + 1):
                pack_lines.append(f"{indent}{table_reg}[{idx}] = {rname}")

            ending = lines[i][len(stripped):]
            for pl in pack_lines:
                result.append(pl + ending)

            stats['fixed'] += 1
        else:
            # Couldn't determine table creation, use index assignments
            reg_names = [v[1] for v in sorted(values, key=lambda x: x[0])]
            pack_lines = []
            for idx, rname in enumerate(reg_names, 1):
                pack_lines.append(f"{indent}{table_reg}[{idx}] = {rname}")

            ending = lines[i][len(stripped):]
            for pl in pack_lines:
                result.append(pl + ending)

            stats['fixed'] += 1

        i += 1
        continue

    return result, stats


# --- TARGETED PATCHES (Pass 3) ---
# Fixes for specific decompiler artifacts that general passes can't handle.
# These run AFTER inversions and setlists are already fixed.

# 3a: Namecall hook varargs passthrough
# The decompiler reconstructs `return oldNc(_p1, ...)` as three separate
# register assignments that only capture the first vararg value, breaking
# every Instance method call with 2+ arguments.
NAMECALL_VARARGS_RE = re.compile(
    r'(\s*)(_\w+) = oldNc\n'
    r'\s*(_\w+) = _p1\n'
    r'\s*(_\w+) = \.\.\.\n'
    r'\s*return \2\(\3, \4\)'
)
NAMECALL_VARARGS_REPL = r'\1return oldNc(_p1, ...)'

# 3b: Game-ready wait loop missing break
# Inside the while-true game-ready check loop, the decompiler drops the
# `break` when _r65() returns true. The pattern after inversion fix is:
#   _rN = _r65 / _rN = _rN() / if not _rN then [empty] end
# Should be: if _rN then break end
# ONLY matches inside while-true loops (checked via context scan).
GAMEREADY_BREAK_RE = re.compile(
    r'((\s*)(_\w+) = _r65\n'
    r'\s*\3 = \3\(\)\n'
    r'\s*)if not \3 then\n'
    r'(\s*end)'
)

def _gameready_break_replace(text):
    """Replace game-ready break pattern only when inside a while-true loop."""
    count = 0
    result = text
    offset = 0
    for m in list(GAMEREADY_BREAK_RE.finditer(text)):
        # Check context: is this inside a while-true-do?
        start = m.start()
        context = text[max(0, start - 1000):start]
        # Count unmatched while-true-do vs end to see if we're inside one
        if 'while true do' not in context:
            continue
        indent = m.group(2)
        reg = m.group(3)
        repl = f"{m.group(1)}if {reg} then\n{indent}    break\n{m.group(4)}"
        result = result[:m.start() + offset] + repl + result[m.end() + offset:]
        offset += len(repl) - len(m.group(0))
        count += 1
    return result, count

# 3c: Stand farm _G.YBAItemFarm bridge
# startStandFarm/stopStandFarm are stored on internal _r152 table but
# never registered on _G.YBAItemFarm, so the Farm Coordinator can't call them.
# Inject registration + isStandFarmRunning helper after the _r152 assignments.
STANDFARM_BRIDGE_RE = re.compile(
    r'(_r175 = "stopStandFarm"\n'
    r'(\s*)_r176 = stopStandFarm\n'
    r'\s*_r152\[_r175\] = _r176\n)'
)
STANDFARM_BRIDGE_REPL = (
    r'\1'
    r'\2if _G.YBAItemFarm then\n'
    r'\2    _G.YBAItemFarm.isStandFarmRunning = function() return _r152.standFarmRunning == true end\n'
    r'\2    _G.YBAItemFarm.startStandFarm = startStandFarm\n'
    r'\2    _G.YBAItemFarm.stopStandFarm = stopStandFarm\n'
    r'\2end\n'
)

# 3d: Game-ready initial check -- skip warn+wait when already ready
def _gameready_initial_skip(text):
    lines = text.split('\n')
    count = 0
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if '"Waiting for strict main game' not in s:
            i += 1
            continue
        # Look back for: end (closing premature if-not block)
        end_idx = None
        for back in range(1, 4):
            if i - back >= 0 and lines[i - back].strip() == 'end':
                end_idx = i - back
                break
        if end_idx is None:
            i += 1
            continue
        # Check pattern above end: if not REG then / REG = _r20
        body_idx = end_idx - 1
        if_idx = end_idx - 2
        if body_idx < 0 or if_idx < 0:
            i += 1
            continue
        body_s = lines[body_idx].strip()
        if_s = lines[if_idx].strip()
        m_body = re.match(r'(_\w+) = _r20$', body_s)
        m_if = re.match(r'if not (_\w+) then$', if_s)
        if not m_body or not m_if or m_body.group(1) != m_if.group(1):
            i += 1
            continue
        reg = m_if.group(1)
        # Verify REG = _r65 / REG = REG() above the if
        check_idx = if_idx - 2
        call_idx = if_idx - 1
        if check_idx < 0:
            i += 1
            continue
        if lines[check_idx].strip() != f'{reg} = _r65':
            i += 1
            continue
        if lines[call_idx].strip() != f'{reg} = {reg}()':
            i += 1
            continue
        # Found the pattern. Find the outer while-true loop after warn+tick
        wloop_start = None
        for j in range(i, min(i + 8, len(lines))):
            if 'while true do' in lines[j].strip():
                wloop_start = j
                break
        if wloop_start is None:
            i += 1
            continue
        # Close the if-not block right BEFORE the outer while loop.
        # The warn+tick only run when game not ready; the while loop
        # runs either way (it checks _r65() and breaks immediately if ready).
        if_indent = lines[if_idx][:len(lines[if_idx]) - len(lines[if_idx].lstrip())]
        lines[end_idx] = ''  # delete premature end
        lines.insert(wloop_start, f'{if_indent}end')  # close if-not before while
        count += 1
        break
    return '\n'.join(lines), count



def apply_targeted_patches(text):
    """Apply targeted fixes for specific decompiler artifacts."""
    stats = {}

    text, n = NAMECALL_VARARGS_RE.subn(NAMECALL_VARARGS_REPL, text)
    stats['namecall_varargs'] = n

    text, n = _gameready_break_replace(text)
    stats['gameready_break'] = n

    text, n = STANDFARM_BRIDGE_RE.subn(STANDFARM_BRIDGE_REPL, text)
    stats['standfarm_bridge'] = n

    return text, stats


# --- BROKEN IF/ELSE FIX ---
# The decompiler places the default-value assignment outside the if block:
#   if [not] VAR then / end / VAR = VALUE   (assignment always executes)
# Fix: move the assignment inside:
#   if [not] VAR then / VAR = VALUE / end   (assignment only when condition holds)

BARE_IF_RE = re.compile(r'^(\s*)if\s+(?:not\s+)?(_r\d+|_p\d+)\s+then\s*$')
BARE_END_RE = re.compile(r'^(\s*)end\s*$')

def fix_broken_if_else(lines):
    """Move assignments back inside their if blocks."""
    result = []
    i = 0
    count = 0

    while i < len(lines):
        if i + 2 >= len(lines):
            result.append(lines[i])
            i += 1
            continue

        s1 = lines[i].rstrip('\r\n')
        s2 = lines[i + 1].rstrip('\r\n')
        s3 = lines[i + 2].rstrip('\r\n')

        m_if = BARE_IF_RE.match(s1)
        if not m_if:
            result.append(lines[i])
            i += 1
            continue

        indent = m_if.group(1)
        var = m_if.group(2)

        m_end = BARE_END_RE.match(s2)
        if not m_end or m_end.group(1) != indent:
            result.append(lines[i])
            i += 1
            continue

        m_assign = re.match(
            r'^' + re.escape(indent) + re.escape(var) + r'\s*=\s*(.+)$', s3
        )
        if not m_assign:
            result.append(lines[i])
            i += 1
            continue

        # Check if the NEXT line calls VAR (VAR = VAR(...) or VAR = VAR.X).
        # If so, the assignment is a new computation (e.g. VAR = getgenv; VAR = VAR()),
        # NOT a default value — it must stay unconditional.
        skip = False
        if i + 3 < len(lines):
            s4 = lines[i + 3].rstrip('\r\n')
            call_pat = r'^\s*' + re.escape(var) + r'\s*=\s*' + re.escape(var) + r'[\.(]'
            if re.match(call_pat, s4):
                skip = True

        if skip:
            # Keep the empty if block and unconditional assignment as-is
            result.append(lines[i])
            result.append(lines[i + 1])
            result.append(lines[i + 2])
            i += 3
            continue

        ending = lines[i][len(s1):]
        result.append(lines[i])
        result.append(f"{indent}    {var} = {m_assign.group(1)}{ending}")
        result.append(lines[i + 1])
        count += 1
        i += 3

    return result, count


# --- VARARGS PASSTHROUGH FIX ---
# The decompiler captures ... into a register and passes only the first value:
#   [local] _rK = ...  / return fn(_rK)    (drops extra args)
# Fix: remove capture line, replace register with ... in the call:
#   return fn(...)

VARARGS_CAPTURE_RE = re.compile(r'^(\s*)(?:local\s+)?(_r\d+)\s*=\s*\.\.\.\s*$')

def fix_varargs_passthrough(lines):
    """Replace single-register varargs capture with direct ... forwarding."""
    result = []
    i = 0
    count = 0

    while i < len(lines):
        if i + 1 >= len(lines):
            result.append(lines[i])
            i += 1
            continue

        s1 = lines[i].rstrip('\r\n')
        m = VARARGS_CAPTURE_RE.match(s1)
        if not m:
            result.append(lines[i])
            i += 1
            continue

        var = m.group(2)
        s2 = lines[i + 1].rstrip('\r\n')

        pattern = re.escape(var) + r'\)'
        if re.search(pattern, s2):
            new_s2 = re.sub(pattern, '...)', s2, count=1)
            ending = lines[i + 1][len(s2):]
            result.append(new_s2 + ending)
            count += 1
            i += 2
        else:
            result.append(lines[i])
            i += 1

    return result, count


# --- TIMEOUT BREAK FIX ---
# Inside while-true loops, the decompiler drops break from timeout checks:
#   if NUMBER < _rN then / end       (empty body)
# Fix: insert the missing break:
#   if NUMBER < _rN then / break / end

TIMEOUT_BREAK_RE = re.compile(
    r'^(\s*)(if\s+\d+(?:\.\d+)?\s*<\s*_r\d+\s+then)\s*\n'
    r'(\s*end\b)',
    re.MULTILINE
)

def fix_timeout_breaks(text):
    """Insert missing break in timeout threshold checks (only inside loops)."""
    count = 0
    result = text
    offset = 0
    for m in list(TIMEOUT_BREAK_RE.finditer(text)):
        start = m.start()
        context = text[max(0, start - 1500):start]
        if 'while true do' not in context:
            continue
        indent = m.group(1)
        repl = f"{indent}{m.group(2)}\n{indent}    break\n{m.group(3)}"
        result = result[:m.start() + offset] + repl + result[m.end() + offset:]
        offset += len(repl) - len(m.group(0))
        count += 1
    return result, count


# --- BRANDING FIX ---
# Rename "YBA Hub" to "YBA" / "YBA Script" in user-facing strings.
# Internal identifiers (YBAHubToggle, YBAHubWindow, etc.) are kept as-is.

BRANDING_REPLACEMENTS = [
    ('YBA Hub - Standalone Deobfuscated Edition', 'YBA Script - Standalone Edition'),
    ('[YBA Hub]', '[YBA]'),
    ('YBA Hub | Your Bizarre Adventure', 'YBA | Your Bizarre Adventure'),
    ('"YBA Hub"', '"YBA Script"'),
    ('username = "YBA Hub"', 'username = "YBA Script"'),
    ('[YBA Hub Webhook]', '[YBA Webhook]'),
    ('initializing YBAHub UI', 'initializing YBA UI'),
    ('"YBAHub/boot_log.txt"', '"YBA/boot_log.txt"'),
]
FOLDER_NAME_RE = re.compile(r'(_r\d+ = "YBAHub)"(\s*$)', re.MULTILINE)

def fix_branding(text):
    """Replace user-facing YBA Hub branding with YBA."""
    count = 0
    for old, new in BRANDING_REPLACEMENTS:
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            count += n
    text, n = FOLDER_NAME_RE.subn(r'\1"', text)
    # Folder-name only: bare "YBAHub" as rvalue → "YBA"
    old_folder = '= "YBAHub"'
    new_folder = '= "YBA"'
    n = text.count(old_folder)
    if n:
        text = text.replace(old_folder, new_folder)
        count += n
    return text, count


# --- KEY SYSTEM STUBS ---
# Replace the key-checking functions (_r165-_r168) with stubs that always
# return "lifetime/paid" values, bypassing the key validation system.
# Uses line scanning with nesting depth to find function boundaries.

def fix_key_stubs(text):
    """Replace key-checking functions with bypass stubs."""
    lines = text.splitlines(True)
    # Find the sequence: _r165 = function(_p1) ... _r166 = function() ...
    # _r167 = function() ... _r168 = function() ... _r169 = nil
    # at the specific indent level where they're the key system functions.
    # Anchor: _r165 with param _p1 followed eventually by _r166, _r167, _r168
    # then _r169 = nil
    key_funcs = ['_r165', '_r166', '_r167', '_r168']
    start_idx = None
    end_idx = None
    indent = None

    i = 0
    while i < len(lines):
        s = lines[i].rstrip('\r\n')
        m = re.match(r'^(\s*)_r165\s*=\s*function\(_p1\)\s*$', s)
        if not m:
            i += 1
            continue

        candidate_indent = m.group(1)
        # Check that _r166, _r167, _r168 follow at the same indent
        # by scanning forward, tracking function nesting
        start_idx = i
        indent = candidate_indent
        func_idx = 0  # currently inside _r165
        depth = 1  # we're inside _r165's function body
        j = i + 1
        found_all = False

        while j < len(lines):
            sj = lines[j].rstrip('\r\n').strip()
            # Count nesting (approximate but works for decompiler output)
            depth += sj.count(' function(') + sj.count(' function ')
            if sj.startswith('function(') or sj.startswith('function '):
                depth += 1
            if re.search(r'=\s*function\s*\(', sj):
                depth += 1
                depth -= sj.count(' function(') + sj.count(' function ')
            # Actually, let me use a simpler approach: count block keywords
            for kw in ['function(', 'function ', 'do\n', 'do ', 'then\n', 'then ']:
                pass  # too complex
            # Simplest: just count 'end' at the right indent
            if sj == 'end' and lines[j].rstrip('\r\n').startswith(indent) and \
               len(lines[j].rstrip('\r\n')) - len(lines[j].rstrip('\r\n').lstrip()) == len(indent):
                # This 'end' is at our indent level - closes current function
                func_idx += 1
                if func_idx >= 4:
                    # All four functions closed
                    end_idx = j + 1
                    found_all = True
                    break
                # Check next line is the expected next function
                if j + 1 < len(lines):
                    next_s = lines[j + 1].rstrip('\r\n')
                    expected = key_funcs[func_idx]
                    if not re.match(r'^' + re.escape(indent) + re.escape(expected) + r'\s*=\s*function', next_s):
                        break  # Not the key system block
            j += 1

        if found_all:
            break
        start_idx = None
        i += 1

    if start_idx is None or end_idx is None:
        return text, 0

    stubs = (
        f'{indent}_r165 = function(_p1)\n'
        f'{indent}    return true\n'
        f'{indent}end\n'
        f'{indent}_r166 = function()\n'
        f'{indent}    return "LIFETIME"\n'
        f'{indent}end\n'
        f'{indent}_r167 = function()\n'
        f'{indent}    return {{key = "LIFETIME", is_lifetime = true, lifetime = true, permanent = true, expires_at = 0, folder_name = "paid"}}\n'
        f'{indent}end\n'
        f'{indent}_r168 = function()\n'
        f'{indent}    return "Lifetime", "Active (Paid Lifetime)", false, nil, "paid", "paid"\n'
        f'{indent}end\n'
    )
    new_lines = lines[:start_idx] + [stubs] + lines[end_idx:]
    return ''.join(new_lines), 1


WHILE_TRUE_RE = re.compile(r'^(\s*)while true do\s*$')
IF_GUARD_RE = re.compile(r'^(\s*)if\s+(not\s+)?(_r\d+|_p\d+)\s+then\s*$')


def fix_missing_breaks(lines):
    """Add missing break statements to while-true loops that have no exit."""
    result = list(lines)
    guard_breaks = 0
    safety_yields = 0

    i = 0
    while i < len(result):
        line = result[i]
        m_while = WHILE_TRUE_RE.match(line.rstrip('\n'))
        if not m_while:
            i += 1
            continue

        w_indent = m_while.group(1)
        w_indent_len = len(w_indent)

        # Find the matching end for this while loop
        depth = 1
        j = i + 1
        has_break = False
        while j < len(result) and depth > 0:
            s = result[j].strip()
            jj_indent = len(result[j]) - len(result[j].lstrip())
            if s == 'end' and jj_indent == w_indent_len:
                depth -= 1
                if depth == 0:
                    break
            elif s == 'end':
                pass
            if jj_indent > w_indent_len:
                if s == 'break':
                    has_break = True
            j += 1

        if has_break:
            i += 1
            continue

        while_end = j  # line index of the while's 'end'
        body_indent = w_indent + '    '

        # Check pattern A: first non-blank line after while is if [not] VAR then,
        # and the if's end is right before the while's end
        first_body = i + 1
        while first_body < while_end and not result[first_body].strip():
            first_body += 1

        if first_body < while_end:
            m_guard = IF_GUARD_RE.match(result[first_body].rstrip('\n'))
            if m_guard:
                g_indent = m_guard.group(1)
                negated = bool(m_guard.group(2))
                var = m_guard.group(3)

                # Find the guard if's matching end
                gdepth = 1
                k = first_body + 1
                while k < while_end and gdepth > 0:
                    s = result[k].strip()
                    kk_indent = len(result[k]) - len(result[k].lstrip())
                    if s == 'end' and kk_indent == len(g_indent):
                        gdepth -= 1
                    elif kk_indent == len(g_indent):
                        for kw in ['if ', 'while ', 'for ', 'function(', 'function ']:
                            if s.startswith(kw) and (s.endswith('then') or s.endswith('do') or 'function' in s):
                                gdepth += 1
                                break
                    k += 1

                guard_end = k - 1

                # Check if guard end is right before while end (no code between)
                between_empty = True
                for m in range(guard_end + 1, while_end):
                    if result[m].strip():
                        between_empty = False
                        break

                if between_empty:
                    # Add break for opposite condition
                    if negated:
                        break_line = f'{body_indent}if {var} then break end\n'
                    else:
                        break_line = f'{body_indent}if not {var} then break end\n'
                    result.insert(i + 1, break_line)
                    guard_breaks += 1
                    i += 2
                    continue

        # Pattern B: no simple guard. Add task.wait(0) safety yield at loop top.
        result.insert(i + 1, f'{body_indent}task.wait(0)\n')
        safety_yields += 1
        i += 2

    return result, guard_breaks, safety_yields


def fix_players_upvalue(lines):
    """Fix all .LocalPlayer accesses to use game:GetService('Players') directly."""
    result = list(lines)
    count = 0

    # 1. Insert _players_svc after the initial Players service assignment
    # Pattern: _r3 = _r3(_r4, _r5) at 4-space indent (line ~28)
    for i in range(min(50, len(result))):
        if re.match(r'^\s{4}_r3 = _r3\(_r4, _r5\)\s*$', result[i]):
            result.insert(i + 1, '    local _players_svc = _r3\n')
            count += 1
            break

    # 2. Replace _r48 function with a clean one-liner
    for i in range(len(result)):
        if result[i].strip() == '_r48 = function()':
            indent = result[i][:len(result[i]) - len(result[i].lstrip())]
            indent_len = len(indent)
            # Find the matching end at same indent level
            j = i + 1
            while j < len(result):
                s = result[j].strip()
                jj_indent = len(result[j]) - len(result[j].lstrip())
                if s == 'end' and jj_indent == indent_len:
                    break
                j += 1
            # Replace from i to j (inclusive)
            replacement = (
                f'{indent}_r48 = function()\n'
                f'{indent}    return game:GetService("Players").LocalPlayer\n'
                f'{indent}end\n'
            )
            result[i:j + 1] = [replacement]
            count += 1
            break

    # 3. Replace ALL _rN = _rM.LocalPlayer with game:GetService("Players").LocalPlayer
    # Catches both self-refs (_rN = _rN.LocalPlayer) and cross-refs (_rN = _rM.LocalPlayer)
    lp_re = re.compile(r'^(\s+)(_r\d+)\s*=\s*(_r\d+)\.LocalPlayer\s*$')
    i = 0
    while i < len(result):
        m = lp_re.match(result[i])
        if m:
            indent = m.group(1)
            target = m.group(2)
            result[i] = f'{indent}{target} = game:GetService("Players").LocalPlayer\n'
            count += 1
        i += 1

    return result, count


# --- DROPPED RETURN-FALSE FIX ---
# The decompiler drops "VAR = false; do return VAR end" outside its if block.
# Pattern (after other passes):
#   if CONDITION then
#   end
#   [zero or more `end` lines closing outer blocks]
#   VAR = false
#   do return VAR end
# Fix: move the two lines inside the empty if block.

RETURN_FALSE_RE = re.compile(r'^(\s*)(_r\d+)\s*=\s*false\s*$')
DO_RETURN_RE = re.compile(r'^(\s*)do return (_r\d+) end\s*$')

def fix_dropped_returns(lines):
    """Move dropped return-false blocks back inside their empty if blocks."""
    result = list(lines)
    count = 0
    i = 0
    while i < len(result):
        # Look for: VAR = false
        s = result[i].rstrip('\r\n')
        m_assign = RETURN_FALSE_RE.match(s)
        if not m_assign:
            i += 1
            continue

        assign_indent = m_assign.group(1)
        var = m_assign.group(2)

        # Check next line is: do return VAR end
        if i + 1 >= len(result):
            i += 1
            continue
        s_ret = result[i + 1].rstrip('\r\n')
        m_ret = DO_RETURN_RE.match(s_ret)
        if not m_ret or m_ret.group(2) != var or m_ret.group(1) != assign_indent:
            i += 1
            continue

        # Look backwards for the nearest empty if block: if COND then / end
        # Only `end` lines allowed between the empty block and our assignment.
        # The if condition must reference the same variable as the assignment.
        found = False
        for k in range(i - 1, max(-1, i - 8), -1):
            sk = result[k].rstrip('\r\n')
            sk_s = sk.strip()
            if sk_s == 'end' and k > 0:
                prev = result[k - 1].rstrip('\r\n')
                prev_s = prev.strip()
                prev_indent = len(prev) - len(prev.lstrip())
                sk_indent = len(sk) - len(sk.lstrip())
                if (prev_indent == sk_indent and
                    var in prev_s and
                    (re.match(r'if\s+.+\s+then$', prev_s) or
                     re.match(r'elseif\s+.+\s+then$', prev_s))):
                    # Found empty if block at k-1, k with matching variable
                    inner_indent = ' ' * (prev_indent + 4)
                    ending = result[i][len(s):]
                    new_assign = f'{inner_indent}{var} = false{ending}'
                    new_return = f'{inner_indent}do return {var} end{ending}'
                    # Insert before the 'end' at line k
                    result[k:k] = [new_assign, new_return]
                    # Original lines at i, i+1 are now at i+2, i+3
                    del result[i + 2:i + 4]
                    count += 1
                    found = True
                    break
            elif sk_s != 'end' and sk_s != '':
                break

        i += 1

    return result, count


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'yba_hub_deobfuscated.lua'
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'yba_fixed.lua'

    with open(input_file, 'r', encoding='latin-1') as f:
        lines = f.readlines()

    print(f"Read {len(lines)} lines from {input_file}")

    # Pass 1: Fix inversions
    inv_count = 0
    fixed_lines = []
    for line in lines:
        new_line = fix_inversion(line)
        if new_line != line:
            inv_count += 1
        fixed_lines.append(new_line)

    print(f"Pass 1: Fixed {inv_count} truthiness inversions")

    # Pass 2: Fix setlists
    fixed_lines, sl_stats = fix_setlists(fixed_lines)

    print(f"Pass 2: Fixed {sl_stats['fixed']} setlist bugs, skipped {sl_stats['skipped']}")

    # Pass 3: Targeted patches (operate on full text, not line-by-line)
    full_text = ''.join(fixed_lines)
    full_text, tp_stats = apply_targeted_patches(full_text)
    fixed_lines = full_text.splitlines(True)

    tp_total = sum(tp_stats.values())
    print(f"Pass 3: {tp_total} targeted patches applied:")
    for name, count in tp_stats.items():
        print(f"  - {name}: {count}")

    # Pass 4: Fix broken if/else reconstruction
    fixed_lines, ie_count = fix_broken_if_else(fixed_lines)
    print(f"Pass 4: Fixed {ie_count} broken if/else blocks")

    # Pass 5: Fix varargs passthrough wrappers
    fixed_lines, va_count = fix_varargs_passthrough(fixed_lines)
    print(f"Pass 5: Fixed {va_count} varargs passthrough wrappers")

    # Pass 6: Fix timeout breaks
    full_text = ''.join(fixed_lines)
    full_text, tb_count = fix_timeout_breaks(full_text)
    fixed_lines = full_text.splitlines(True)
    print(f"Pass 6: Fixed {tb_count} timeout break statements")

    # Pass 7: Branding
    full_text = ''.join(fixed_lines)
    full_text, br_count = fix_branding(full_text)
    fixed_lines = full_text.splitlines(True)
    print(f"Pass 7: Fixed {br_count} branding strings")

    # Pass 8: Key system stubs
    full_text = ''.join(fixed_lines)
    full_text, ks_count = fix_key_stubs(full_text)
    fixed_lines = full_text.splitlines(True)
    print(f"Pass 8: Replaced {ks_count} key system function(s) with stubs")

    # Pass 9: Fix missing breaks in while-true loops
    fixed_lines, gb_count, sy_count = fix_missing_breaks(fixed_lines)
    print(f"Pass 9: Fixed {gb_count} guard-pattern breaks, {sy_count} safety yields")

    # Pass 10: Fix _r3 Players upvalue references
    fixed_lines, pu_count = fix_players_upvalue(fixed_lines)
    print(f"Pass 10: Fixed {pu_count} Players service upvalue references")

    # Pass 11: Fix dropped return-false blocks
    fixed_lines, dr_count = fix_dropped_returns(fixed_lines)
    print(f"Pass 11: Fixed {dr_count} dropped return-false blocks")

    # Pass 12: Game-ready initial skip (must run after Pass 4 creates the if-not block)
    full_text = ''.join(fixed_lines)
    full_text, gis_count = _gameready_initial_skip(full_text)
    fixed_lines = full_text.splitlines(True)
    print(f"Pass 12: Fixed {gis_count} game-ready initial skip")

    with open(output_file, 'w', encoding='latin-1') as f:
        f.writelines(fixed_lines)

    total = inv_count + sl_stats['fixed'] + tp_total + ie_count + va_count + tb_count + br_count + ks_count + gb_count + sy_count + pu_count + dr_count + gis_count
    print(f"\nTotal: {total} fixes applied")
    print(f"Output written to {output_file} ({len(fixed_lines)} lines)")


if __name__ == '__main__':
    main()
