#!/usr/bin/env python3
"""
Fix decompiler bugs in YBA deobfuscated Lua script.

Passes run in this order:
0b. Structural repairs: four one-off blocks the decompiler left open to the end of the file
    (selectedFarmItems cleanup loop, anti-teleport bypass else, game-ready GameModes or-chain,
    outer `while true do` around the game-ready wait).
0.  TESTSET restoration: `X = Y` / `if [not] X then` where Y is the function's own register
    or param is printed with the correct sense; mark it so Pass 1 leaves it, and move its
    right operand / lost ternary else into place.
1.  Truthiness inversions: flips bare `if [not] VAR then` / `elseif [not] VAR then` (TEST).
    Does NOT touch == ~= < > <= >= comparisons (those aren't inverted by the decompiler).
1b. Boolean materializations: `x = (a == b)` whose lost `else x = true` nested the rest of
    the block inside the if.
2.  Setlist bugs: replaces `-- setlist _rN (batch=K)` comments with table packing.
2b. Multi-return forwarding: `f(g())` printed with a fixed register range of stale values.
3.  Targeted patches: namecall varargs, game-ready break, stand farm bridge, first-spawn loop,
    UI visibility restore loop.
3b. Or-chains: `if a or b then REST end` printed as nested tests with an empty innermost if.
4.  Broken if/else: moves dropped right operands (`x = x or v`) and guarded call bodies
    (`if not c then pcall(fn) end`) back inside their empty ifs.
5.  Varargs passthrough: replaces register captures of ... with direct forwarding.
6.  Timeout breaks: inserts missing break in `if NUMBER < _rN then end` patterns.
7.  Branding: renames YBA Hub to YBA / YBA Script in user-facing strings.
8.  Key stubs: replaces key-checking functions with bypass stubs.
8b. While-condition chains: `while A and B do ... end` printed without its exits.
8c. Loop tail exits: search loops whose break was dropped and whose wait went into an else.
9.  Missing breaks in remaining while-true loops + safety yields.
10. Players service upvalue fixes.
11. Dropped return-false blocks (single tests; chains are handled by 3b).
12. Game-ready initial skip.
13. Scope leaks: registers declared `local` inside a nested block but used outside it are
    hoisted to function scope; registers sharing a name with an upvalue (including a closure's
    own call-frame temporaries, and copies of a register a call has clobbered) are renamed.
13b. Closed upvalues: main-chunk registers reused after the closures that captured them get a
    new name for the captured range.
14. Garbled empty strings: 2-byte garbage constants that stand for "".
"""

import re
import sys
from collections import defaultdict

def split_lines(text):
    """Split on newlines only, keeping line endings. str.splitlines() also splits on
    other separators such as 0x85, which occurs inside latin-1 string literals here."""
    parts = text.split('\n')
    out = [p + '\n' for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


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
    """Flip a bare truthiness test if present (TESTSET-marked tests are already correct)."""
    if TS_MARK in line:
        return line
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

        # General case: pack values into table.
        # SETLIST stores the final values of registers t+1..L, where L is the register written
        # by the last assignment before it. Registers are reused while elements are computed
        # (temporaries, call arguments above L), so each register is packed once, in order.
        # For batch=1: _rN = {val1, val2, ...} or _rN[1], _rN[2] = val1, val2
        # For batch>1: append at offset

        if batch == 1 and found_table_creation:
            # We can replace the table creation with an initializer
            # But only if the values are simple register references
            # For safety, use explicit index assignment
            reg_names = [f'_r{n}' for n in range(tnum + 1, values[-1][0] + 1)]

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

            reg_names = [f'_r{n}' for n in range(tnum + 1, values[-1][0] + 1)]
            pack_lines = []
            for idx, rname in enumerate(reg_names, offset + 1):
                pack_lines.append(f"{indent}{table_reg}[{idx}] = {rname}")

            ending = lines[i][len(stripped):]
            for pl in pack_lines:
                result.append(pl + ending)

            stats['fixed'] += 1
        else:
            # Couldn't determine table creation, use index assignments
            reg_names = [f'_r{n}' for n in range(tnum + 1, values[-1][0] + 1)]
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



# 3e: First-spawn wait (`_r79`): `while not (_r65 and _r65()) do task.wait(0.5) end` followed
# by one-time first-spawn code. The decompiler put that code inside the loop's inner if and
# dropped the exit, so once in game it re-ran (log, _r73(), _r69()) every frame. Rebuild the
# loop with its break and move the first-spawn block after it.
FIRST_SPAWN_HEAD = [
    '_r79 = function()', '    while true do', '        local _r0 = _r65', '        if _r0 then',
    '            _r0 = _r65', '            _r0 = _r0()', '            if _r0 then',
]
FIRST_SPAWN_TAIL = [
    '            end', '        else', '            _r0 = task', '            _r0 = _r0.wait',
    '            _r1 = 0.5', '            _r0(_r1)', '        end', '    end',
]


def _first_spawn_loop(text):
    lines = split_lines(text)
    k = next((n for n, ln in enumerate(lines)
              if '"FIRST SPAWN (strict main game) reached"' in ln), None)
    if k is None:
        return text, 0
    h = k
    while h > 0 and lines[h].strip() != '_r79 = function()':
        h -= 1
    base = lines[h][:_indent(lines[h])]
    n = len(FIRST_SPAWN_HEAD)
    assert [ln.rstrip('\r\n') for ln in lines[h:h + n]] == [base + s for s in FIRST_SPAWN_HEAD], \
        'first-spawn loop: unexpected head'
    t = h + n
    while lines[t].rstrip('\r\n') != base + FIRST_SPAWN_TAIL[0]:
        t += 1
    m = len(FIRST_SPAWN_TAIL)
    assert [ln.rstrip('\r\n') for ln in lines[t:t + m]] == [base + s for s in FIRST_SPAWN_TAIL], \
        'first-spawn loop: unexpected tail'
    body = [ln[12:] if ln.startswith(base + ' ' * 16) else ln for ln in lines[h + n:t]]
    loop = [base + s + '\n' for s in (
        '_r79 = function()', '    while true do', '        local _r0 = _r65', '        if _r0 then',
        '            _r0 = _r65', '            _r0 = _r0()', '        end', '        if _r0 then',
        '            break', '        end', '        _r0 = task', '        _r0 = _r0.wait',
        '        _r1 = 0.5', '        _r0(_r1)', '    end')]
    lines[h:t + m] = loop + body
    return ''.join(lines), 1


# 3f: UI visibility restore task. After hiding the window, the task waits up to 300 s for the
# main game and then restores the saved UI_Open state:
#   while tick() - start < 300 do task.wait(1) if _r65 and _r65() then break end end
#   if _r65 and _r65() then <restore> end
# The decompiler nested the loop in three `while true do` and put the restore inside it after
# the break, so the break re-entered the loop forever and the window stayed hidden.
def _ui_restore_loop(text):
    lines = split_lines(text)
    k = next((n for n, ln in enumerate(lines) if ln.strip() == 'if _r16 < 300 then'), None)
    if k is None:
        return text, 0
    b = _indent(lines[k]) - 12
    pad = ' ' * b
    assert [ln.rstrip('\r\n') for ln in lines[k - 6:k - 3]] == \
        [pad + 'while true do', pad + '    while true do', pad + '        while true do'], \
        'ui restore loop: unexpected loop nesting'
    assert lines[k - 1].strip() == '_r16 = _r16 - _r15', 'ui restore loop: elapsed'
    e_if = block_end(lines, k, b + 12)
    body = b + 16
    c = k + 5                                   # after the 4 wait lines
    assert lines[c].strip() == '_r16 = _r65' and lines[c + 1].strip() == 'if _r16 then'
    c_end = block_end(lines, c + 1, body)
    r = c_end + 1
    assert lines[r].strip() == '_r16 = _r65' and lines[r + 1].strip() == 'if _r16 then'
    r_end = block_end(lines, r + 1, body)
    assert lines[r_end + 1].strip() == 'return' and r_end + 2 == e_if, 'ui restore loop: tail'
    assert [ln.strip() for ln in lines[e_if + 1:e_if + 4]] == ['end'] * 3, 'ui restore loop: ends'
    elapsed = lines[k - 3:k]
    wait_check = lines[k + 1:c_end + 1]
    restore = lines[r:r_end + 1]
    _dedent(elapsed, 0, len(elapsed), 8)
    _dedent(wait_check, 0, len(wait_check), 8)
    _dedent(restore, 0, len(restore), 16)
    new = ([pad + 'while true do\n'] + elapsed + [pad + '    if _r16 < 300 then\n'] + wait_check +
           [pad + '    else\n', pad + '        break\n', pad + '    end\n', pad + 'end\n'] +
           restore + [pad + 'return\n'])
    lines[k - 6:e_if + 4] = new
    return ''.join(lines), 1


def apply_targeted_patches(text):
    """Apply targeted fixes for specific decompiler artifacts."""
    stats = {}

    text, n = NAMECALL_VARARGS_RE.subn(NAMECALL_VARARGS_REPL, text)
    stats['namecall_varargs'] = n

    text, n = _gameready_break_replace(text)
    stats['gameready_break'] = n

    text, n = STANDFARM_BRIDGE_RE.subn(STANDFARM_BRIDGE_REPL, text)
    stats['standfarm_bridge'] = n

    text, n = _first_spawn_loop(text)
    stats['first_spawn_loop'] = n

    text, n = _ui_restore_loop(text)
    stats['ui_restore_loop'] = n

    return text, stats


# --- BROKEN IF/ELSE FIX ---
# `x = x or v` / `x = x and v` (TEST in place) decompile to an empty if followed by the
# statements that compute the right operand, which then always execute:
#   if [not] VAR then / end / VAR = ... [/ VAR = VAR(...) ...]
# Fix: move the whole right operand (see operand_extent) inside the if:
#   if [not] VAR then / VAR = ... [/ VAR = VAR(...) ...] / end

BARE_IF_RE = re.compile(r'^(\s*)if\s+(?:not\s+)?(_r\d+|_p\d+)\s+then\s*$')
BARE_END_RE = re.compile(r'^(\s*)end\s*$')

CALL_STMT_RE = re.compile(r'^(?:_r\d+(?:\s*,\s*_r\d+)*\s*=\s*)?_r(\d+)\(.*\)(?:\s*--.*)?$')
BODY_END_RE = re.compile(r'^(?:do return\b.*\bend|return\b.*|break|-- goto \[\d+\])$')


def _guarded_call_end(lines, start, ind, reg):
    """`if not cond then pcall(fn) end` decompiles like `x = x or v`, but the code after the
    empty if loads a function into the tested register and calls it:
        if [not] X then / end / X = pcall / _rK = function() ... end / X(_rK)
    Scan the statements that only touch registers >= X (assignments, closure definitions,
    calls). Return the exclusive end of the guarded body (through the end of the run when it
    reaches the enclosing block's end or a return/break, else through the last call of X),
    or None when X is never called (a plain `x = x or v` operand)."""
    xn = int(reg[2:])
    j = start
    last_call = None
    while j < len(lines):
        s = lines[j].rstrip('\r\n')
        st = s.strip()
        if st and _indent(s) < ind:
            return j if last_call is not None else None      # enclosing block ends
        if _indent(s) != ind:
            break
        m = ASSIGN_REG_RE.match(st)
        if m and int(m.group(1)) >= xn:
            if re.match(r'^function\s*\(', m.group(2)):
                e = block_end(lines, j, ind)
                if e is None:
                    break
                j = e + 1
            else:
                j += 1
            continue
        c = CALL_STMT_RE.match(st)
        if c and int(c.group(1)) >= xn:
            j += 1
            if int(c.group(1)) == xn:
                last_call = j
            continue
        if BODY_END_RE.match(st) and last_call is not None:
            return j + 1
        break
    return last_call


def fix_broken_if_else(lines):
    """Move dropped right operands (or guarded call bodies) back inside their empty if blocks."""
    lines = list(lines)
    count = 0
    for i in range(len(lines) - 3, -1, -1):
        m_if = BARE_IF_RE.match(lines[i].rstrip('\r\n'))
        if not m_if:
            continue
        indent, var = m_if.group(1), m_if.group(2)
        m_end = BARE_END_RE.match(lines[i + 1].rstrip('\r\n'))
        if not m_end or m_end.group(1) != indent:
            continue
        if not re.match(r'^' + re.escape(indent) + re.escape(var) + r'\s*=\s*(.+)$',
                        lines[i + 2].rstrip('\r\n')):
            continue
        if var.startswith('_r'):
            stop = operand_extent(lines, i + 2, len(indent), var)
            body = _guarded_call_end(lines, i + 2, len(indent), var)
            if body is not None and body > stop:
                stop = body
        else:
            stop = i + 3
        if stop <= i + 2:
            continue
        lines[i + 1:stop] = ['    ' + ln for ln in lines[i + 2:stop]] + [lines[i + 1]]
        count += 1
    return lines, count


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
    lines = split_lines(text)
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
SIMPLE_STMT_RE = re.compile(r'^(?:local\s+)?_r\d+(?:\s*,\s*_r\d+)*\s*=\s*[^\n]*$')
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


# --- SCOPE ANALYSIS ---
# The decompiler emits `local _rN` at a register's first assignment even when that
# assignment sits inside a nested block. Later uses outside the block then resolve to a
# global or to an enclosing function's variable of the same name. In the VM every
# register is function-wide, so these are leaks. This section tokenizes the output,
# rebuilds lexical scopes, and finds those leaks (used by Pass 0 and Pass 13).

KEYWORDS = {
    'and', 'break', 'do', 'else', 'elseif', 'end', 'false', 'for', 'function', 'if', 'in',
    'local', 'nil', 'not', 'or', 'repeat', 'return', 'then', 'true', 'until', 'while',
}
REG_RE = re.compile(r'^_[rp]\d+$')

TOKEN_RE = re.compile(r'''
    (?P<ws>[ \t\r]+)
  | (?P<nl>\n)
  | (?P<lcomment>--\[(?P<lceq>=*)\[.*?\](?P=lceq)\])
  | (?P<comment>--[^\n]*)
  | (?P<lstring>\[(?P<lseq>=*)\[.*?\](?P=lseq)\])
  | (?P<string>"(?:\\.|\\\n|[^"\\\n])*"|'(?:\\.|\\\n|[^'\\\n])*')
  | (?P<number>0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\.\.\.|\.\.|==|~=|<=|>=|::|[-+*/%^#<>=(){}\[\];:,.])
''', re.S | re.X)


def tokenize(text):
    """Yield (kind, value, line, offset). Strings/comments are dropped but counted for lines."""
    toks = []
    line = 1
    pos = 0
    n = len(text)
    while pos < n:
        m = TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f'cannot tokenize at line {line}: {text[pos:pos + 30]!r}')
        kind = m.lastgroup
        val = m.group(0)
        if kind == 'name':
            toks.append(('kw' if val in KEYWORDS else 'name', val, line, pos))
        elif kind == 'op':
            toks.append(('op', val, line, pos))
        elif kind in ('number', 'string', 'lstring'):
            toks.append(('lit', None, line, pos))
        line += val.count('\n')
        pos = m.end()
    return toks


class Block:
    __slots__ = ('kind', 'locals', 'func', 'line')

    def __init__(self, kind, func, line):
        self.kind, self.func, self.line = kind, func, line
        self.locals = {}


class Func:
    __slots__ = ('parent', 'line', 'blocks', 'decls', 'uses', 'body_tok', 'header_tok', 'end_tok', 'children')

    def __init__(self, parent, line, header_tok):
        self.parent, self.line, self.header_tok = parent, line, header_tok
        self.blocks, self.decls, self.uses, self.children = [], [], [], []
        self.body_tok = None
        self.end_tok = None


class Decl:
    __slots__ = ('name', 'line', 'tok', 'block', 'is_param', 'captured_by')

    def __init__(self, name, line, tok, block, is_param=False):
        self.name, self.line, self.tok, self.block, self.is_param = name, line, tok, block, is_param
        self.captured_by = []


def _resolve(F, name):
    for b in reversed(F.blocks):
        d = b.locals.get(name)
        if d is not None:
            return 'own', d
    G = F.parent
    while G is not None:
        for b in reversed(G.blocks):
            d = b.locals.get(name)
            if d is not None:
                return 'up', d
        G = G.parent
    return 'global', None


def analyze(toks):
    """Walk tokens, build function/block scopes, record register declarations and uses."""
    main = Func(None, 0, -1)
    main.blocks.append(Block('func', main, 0))
    main.body_tok = 0
    funcs = [main]
    fstack = [main]
    pending = []          # [func, names, line, depth, tok]
    for_pending = []      # stack of (block, names) waiting for their header 'do'
    until_line = None
    n = len(toks)
    i = 0
    while i < n:
        kind, val, line, _ = toks[i]
        F = fstack[-1]
        if pending:
            keep = []
            for p in pending:
                if line > p[2] and fstack[-1] is p[0] and len(p[0].blocks) == p[3]:
                    blk = p[0].blocks[-1]
                    for nm in p[1]:
                        d = Decl(nm, p[2], p[4], blk)
                        blk.locals[nm] = d
                        p[0].decls.append(d)
                elif len(p[0].blocks) < p[3] or p[0] not in fstack:
                    pass  # its block closed before activation; drop
                else:
                    keep.append(p)
            pending = keep
        if until_line is not None and line > until_line:
            F.blocks.pop()
            until_line = None
        if kind == 'kw':
            if val == 'function':
                j = i + 1
                is_method = False
                while toks[j][0] == 'name' or (toks[j][0] == 'op' and toks[j][1] in ('.', ':')):
                    if toks[j][1] == ':':
                        is_method = True
                    if toks[j][0] == 'name' and REG_RE.match(toks[j][1]) and j == i + 1:
                        res, d = _resolve(F, toks[j][1])
                        F.uses.append((toks[j][1], toks[j][2], j, res, d))
                    j += 1
                assert toks[j][1] == '(', f'expected ( at line {toks[j][2]}'
                NF = Func(F, line, i)
                root = Block('func', NF, line)
                NF.blocks.append(root)
                if is_method:
                    root.locals['self'] = Decl('self', line, j, root, True)
                j += 1
                while toks[j][1] != ')':
                    if toks[j][0] == 'name':
                        d = Decl(toks[j][1], line, j, root, True)
                        root.locals[toks[j][1]] = d
                    j += 1
                NF.body_tok = j + 1
                F.children.append(NF)
                fstack.append(NF)
                funcs.append(NF)
                i = j + 1
                continue
            if val == 'local':
                if toks[i + 1][1] == 'function':
                    nm = toks[i + 2][1]
                    d = Decl(nm, line, i, F.blocks[-1])
                    F.blocks[-1].locals[nm] = d
                    F.decls.append(d)
                    i += 1
                    continue
                j = i + 1
                names = []
                while toks[j][0] == 'name':
                    names.append(toks[j][1])
                    if toks[j + 1][1] == ',':
                        j += 2
                    else:
                        j += 1
                        break
                pending.append([F, names, line, len(F.blocks), i])
                i = j
                continue
            if val == 'for':
                j = i + 1
                names = []
                while toks[j][1] not in ('=', 'in'):
                    if toks[j][0] == 'name':
                        names.append(toks[j][1])
                    j += 1
                blk = Block('for', F, line)
                F.blocks.append(blk)
                for_pending.append((blk, names, i))
                i = j
                continue
            if val == 'while':
                F.blocks.append(Block('while', F, line))
                for_pending.append((None, [], i))
            elif val == 'do':
                if for_pending:
                    blk, names, ti = for_pending.pop()
                    if blk is not None:
                        for nm in names:
                            d = Decl(nm, line, ti, blk)
                            blk.locals[nm] = d
                            F.decls.append(d)
                else:
                    F.blocks.append(Block('do', F, line))
            elif val == 'then':
                F.blocks.append(Block('then', F, line))
            elif val == 'elseif':
                F.blocks.pop()
            elif val == 'else':
                F.blocks.pop()
                F.blocks.append(Block('else', F, line))
            elif val == 'repeat':
                F.blocks.append(Block('repeat', F, line))
            elif val == 'until':
                until_line = line
            elif val == 'end':
                F.blocks.pop()
                if not F.blocks:
                    F.end_tok = i
                    fstack.pop()
        elif kind == 'name' and REG_RE.match(val):
            prev = toks[i - 1]
            if not (prev[0] == 'op' and prev[1] in ('.', ':')):
                res, d = _resolve(F, val)
                F.uses.append((val, line, i, res, d))
                if res == 'up':
                    d.captured_by.append(F)
        i += 1
    assert len(fstack) == 1, f'unbalanced: {len(fstack)} functions open'
    main.end_tok = n
    return funcs



# --- Per-line helpers (decompiler output has one statement per line) ---

REG_NUM_RE = re.compile(r'^_r(\d+)$')


def own_line_tokens(F, toks):
    """Map line -> token indices in F's own body, excluding nested function spans."""
    spans = sorted((c.header_tok, c.end_tok) for c in F.children)
    out = {}
    lo = F.body_tok
    hi = F.end_tok if F.end_tok is not None else len(toks)
    si = 0
    i = lo
    while i < hi:
        while si < len(spans) and spans[si][1] < i:
            si += 1
        if si < len(spans) and spans[si][0] <= i <= spans[si][1]:
            i = spans[si][1] + 1
            continue
        out.setdefault(toks[i][2], []).append(i)
        i += 1
    return out


def _top_eq(toks, lt):
    """Position (in lt) of the top-level assignment '=', or None."""
    depth = 0
    for pos, ti in enumerate(lt):
        kind, v = toks[ti][0], toks[ti][1]
        if kind != 'op':
            continue
        if v in ('(', '[', '{'):
            depth += 1
        elif v in (')', ']', '}'):
            depth -= 1
        elif v == '=' and depth == 0:
            return pos
    return None


def classify_use(toks, lt, pos):
    """Classify the register occurrence lt[pos]: 'owrite' | 'cwrite' | 'oread' | 'cread'.

    c* = a plain copy (`X = R` / `R = _rX`), the only shape a GETUPVAL/SETUPVAL can take.
    """
    first = toks[lt[0]][1]
    if first == 'for':
        for q, ti in enumerate(lt):
            if toks[ti][1] in ('in', '='):
                return 'owrite' if pos < q else 'oread'
        return 'oread'
    start = 1 if first == 'local' else 0
    eq = _top_eq(toks, lt)
    if start == 1 and (eq is None or pos < eq):
        return 'owrite'          # declaration
    if eq is None:
        return 'oread'
    lhs, rhs = lt[start:eq], lt[eq + 1:]
    if pos < eq:
        nxt = toks[lt[pos + 1]][1]
        if nxt not in (',', '='):
            return 'oread'       # R.x = v / R[k] = v
        if len(lhs) == 1 and len(rhs) == 1 and REG_RE.match(toks[rhs[0]][1] or ''):
            return 'cwrite'
        return 'owrite'
    if len(lhs) == 1 and len(rhs) == 1 and toks[lhs[0]][0] == 'name':
        return 'cread'
    return 'oread'


def call_kill_threshold(toks, lt):
    """For a call statement `[local] [a, b =] _rB(...)`, registers numbered >= B + #results
    hold garbage afterwards. Return that threshold, or None if the line is not such a call."""
    start = 1 if toks[lt[0]][1] == 'local' else 0
    eq = _top_eq(toks, lt)
    if eq is None:
        cpos, nres = start, 0
    else:
        cpos = eq + 1
        nres = sum(1 for ti in lt[start:eq] if toks[ti][0] == 'name')
    if cpos + 1 >= len(lt):
        return None
    m = REG_NUM_RE.match(toks[lt[cpos]][1] or '')
    if not m or toks[lt[cpos + 1]][1] != '(':
        return None
    depth = 0
    for q in range(cpos + 1, len(lt)):
        v = toks[lt[q]][1]
        if v == '(':
            depth += 1
        elif v == ')':
            depth -= 1
            if depth == 0:
                return int(m.group(1)) + nres if q == len(lt) - 1 else None
    return None

def closure_frames(F, toks, lines_F):
    """{child Func: set of register names} for closures created as `[local] _rN = function`
    and passed straight to a call `[...=] _rJ(_rJ+1, ..., _rN, ...)` on the next line.
    Registers J..N-1 hold that call's callee and earlier arguments: temporaries a closure
    cannot capture, so a closure reference to those names is an outer upvalue."""
    out = {}
    for C in F.children:
        lt = lines_F.get(toks[C.header_tok][2], [])
        names = [toks[t][1] for t in lt]
        if names[:1] == ['local']:
            names = names[1:]
        if len(names) != 2 or names[1] != '=' or not REG_NUM_RE.match(names[0] or ''):
            continue
        n = int(names[0][2:])
        end_line = toks[C.end_tok][2]
        nxt = min((ln for ln in lines_F if ln > end_line), default=None)
        if nxt is None:
            continue
        vals = [toks[t][1] for t in lines_F[nxt]]
        if '(' not in vals or vals[-1] != ')':
            continue
        p = vals.index('(')
        m = REG_NUM_RE.match(vals[p - 1] or '')
        args = [v for v in vals[p + 1:-1] if v != ',']
        if not m or not args or names[0] not in args:
            continue
        j = int(m.group(1))
        if [a for a in args] != [f'_r{x}' for x in range(j + 1, j + 1 + len(args))]:
            continue
        out[C] = {f'_r{x}' for x in range(j, n)}
    return out


def _subtree(C):
    out, stack = [], [C]
    while stack:
        G = stack.pop()
        out.append(G)
        stack.extend(G.children)
    return out


def find_leaks(funcs, toks=None):
    """Return {func: {name: {'decls': set(Decl), 'uses': [(line, tok, res)]}}} for leaked registers.

    A use is a leak when it does not resolve to one of the function's own visible locals,
    yet the function declared that register earlier in its own body. With toks given,
    registers a (non-main) function writes with a non-copy write but never declares are
    reported too (their declaration set is empty): such a write can only target the
    function's own register.
    """
    out = {}
    for F in funcs:
        by_name = {}
        for d in F.decls:
            by_name.setdefault(d.name, []).append(d)
        for name, line, tok, res, _ in F.uses:
            if res == 'own' or name not in by_name:
                continue
            earlier = [d for d in by_name[name] if d.tok < tok]
            if not earlier:
                continue
            ent = out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
            ent['decls'].add(earlier[-1])
            ent['uses'].append((line, tok, res))
        if toks is None or F.parent is None:
            continue
        lines_F = own_line_tokens(F, toks)
        # Non-copy writes before a register's first declaration can only target F's own
        # register (an upvalue is written only by a plain copy).
        for name, line, tok, res, _ in F.uses:
            if res == 'own' or name not in by_name or not name.startswith('_r'):
                continue
            lt = lines_F.get(line)
            if lt and tok in lt and tok < min(d.tok for d in by_name[name]) \
                    and classify_use(toks, lt, lt.index(tok)) == 'owrite':
                ent = out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
                ent['uses'].append((line, tok, res))
        # `R = R` / `local R = R`: GETUPVAL of an enclosing variable that shares its name
        # with F's own register R. Always a collision, leak or not.
        for name, line, tok, res, _ in F.uses:
            lt = lines_F.get(line)
            if lt and name.startswith('_r') and [toks[t][1] for t in lt] in (
                    [name, '=', name], ['local', name, '=', name]):
                ent = out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
                ent['uses'].append((line, tok, res))
        # A copy from F's own register R while R is dead (clobbered as a call temporary) is a
        # GETUPVAL of an enclosing variable sharing the name, even inside R's local scope.
        copied = set()
        for name, line, tok, res, _ in F.uses:
            lt = lines_F.get(line)
            if res == 'own' and name in by_name and name.startswith('_r') and lt and tok in lt \
                    and classify_use(toks, lt, lt.index(tok)) == 'cread':
                copied.add(name)
        for name in copied:
            if F in out and name in out[F]:
                continue
            first = min(d.tok for d in by_name[name])
            dec, _ = classify_pair(F, name, toks, lines_F, False)
            if any(v == 'up' and t > first for t, v in dec.items()):
                out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
        # Closure references to call-frame temporaries of F bind to F only by name collision.
        for C, temps in closure_frames(F, toks, lines_F).items():
            for G in _subtree(C):
                for name, line, tok, res, d in G.uses:
                    if name in temps and res != 'own' and d is not None and d.block.func is F:
                        out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
        declared = set(by_name)
        cands = [u for u in F.uses if u[3] != 'own' and u[0] not in declared and u[0].startswith('_r')]
        if not cands:
            continue
        undeclared = set()
        for name, line, tok, res, _ in cands:
            lt = lines_F.get(line)
            if lt and tok in lt and classify_use(toks, lt, lt.index(tok)) == 'owrite':
                undeclared.add(name)
        for name, line, tok, res, _ in cands:
            if name in undeclared:
                ent = out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
                ent['uses'].append((line, tok, res))
    return out


def classify_pair(F, R, toks, lines_F, is_main):
    """Decide, for every occurrence of R in F's own body, whether it is F's own
    register ('own') or the enclosing variable of the same name ('up').

    Returns (decision, timeline). timeline is a list of (last token of line, live) giving
    whether F's own R holds a value after each line that touches R.
    F's register starts at its first declaration, or (for registers the decompiler never
    declared) at its first non-copy write."""
    num = int(R[2:])
    events = []                       # (occurrences [(tok, kind)], kill, last token)
    for line in sorted(lines_F):
        lt = lines_F[line]
        occ = []
        for pos, ti in enumerate(lt):
            if toks[ti][1] == R and toks[ti][0] == 'name':
                prev = toks[ti - 1]
                if prev[0] == 'op' and prev[1] in ('.', ':'):
                    continue
                occ.append((ti, classify_use(toks, lt, pos)))
        thr = call_kill_threshold(toks, lt)
        kill = thr is not None and num >= thr
        if occ or kill:
            events.append((occ, kill, lt[-1]))
    decl_toks = [d.tok for d in F.decls if d.name == R]
    owr = [ti for occ, _, _ in events for ti, k in occ if k == 'owrite']
    first_decl = min(decl_toks + owr) if decl_toks or owr else len(toks)
    decision = {}
    timeline = []
    live = False
    for idx, (occ, kill, last) in enumerate(events):
        if len(occ) == 2 and occ[0][1] in ('cwrite', 'owrite') and occ[1][1] == 'cread' and not is_main:
            # `R = R`: a register is never moved onto itself, so this is GETUPVAL of the
            # enclosing variable named R into F's own register R.
            decision[occ[1][0]] = 'up'
            decision[occ[0][0]] = 'own'
            live = True
            timeline.append((last, live))
            continue
        for ti, k in occ:
            if k not in ('oread', 'cread'):
                continue
            if is_main or (ti > first_decl and (k == 'oread' or live)):
                decision[ti] = 'own'
            else:
                decision[ti] = 'up'
        if kill:
            live = False
        for ti, k in occ:
            if k not in ('owrite', 'cwrite'):
                continue
            if is_main or (ti >= first_decl and k == 'owrite'):
                decision[ti] = 'own'
                live = True
                continue
            if ti < first_decl:
                decision[ti] = 'up'
                continue
            own = False
            for occ2, kill2, _ in events[idx + 1:]:
                if any(k2 in ('oread', 'cread') for _, k2 in occ2):
                    own = True
                    break
                if kill2 or occ2:
                    break
            decision[ti] = 'own' if own else 'up'
            live = live or own
        timeline.append((last, live))
    return decision, timeline


def live_at(timeline, tok):
    """Whether the register is live just before token index tok, per classify_pair's timeline."""
    lo, hi = 0, len(timeline)
    while lo < hi:
        mid = (lo + hi) // 2
        if timeline[mid][0] < tok:
            lo = mid + 1
        else:
            hi = mid
    return timeline[lo - 1][1] if lo else False


def _local_names(toks, ltok):
    """Token indices of the names declared by the `local` statement at ltok."""
    out = []
    j = ltok + 1
    while toks[j][0] == 'name':
        out.append(j)
        if toks[j + 1][1] != ',':
            break
        j += 2
    return out


def fix_scope_leaks(text):
    """Hoist leaked registers to function scope. Returns (new_text, stats, warnings)."""
    toks = tokenize(text)
    funcs = analyze(toks)
    leaks = find_leaks(funcs, toks)
    lines_of = {F: own_line_tokens(F, toks) for F in leaks}
    decisions = {}                      # (F, R) -> ({tok: 'own' | 'up'}, timeline)
    for F, names in leaks.items():
        for R in names:
            if R.startswith('_r'):
                decisions[(F, R)] = classify_pair(F, R, toks, lines_of[F], F.parent is None)
    depth = {}
    for F in funcs:                     # parents precede children in funcs
        depth[F] = 0 if F.parent is None else depth[F.parent] + 1

    # A reference that is not F-own binds to the nearest enclosing function owning R whose
    # register is live where the closure on the path is created (else it is a pass-through
    # upvalue and the next ancestor is checked). Main-chunk registers are globals: no claim.
    claims = {}                         # (G, tok) -> claiming Func
    refs = defaultdict(list)            # R -> [(G, tok)] for every not-own reference
    frame_cache = {}

    def frames_of(A):
        if A not in frame_cache:
            frame_cache[A] = closure_frames(A, toks, lines_of.get(A) or own_line_tokens(A, toks)) \
                if A.children else {}
        return frame_cache[A]
    for G in funcs:
        for name, _, tok, res, d in G.uses:
            if not name.startswith('_r'):
                continue
            gd = decisions.get((G, name))
            if (gd[0].get(tok, 'own' if res == 'own' else 'up') if gd else res) == 'own':
                continue
            refs[name].append((G, tok))
            C, A = G, G.parent
            while A is not None and A.parent is not None:
                if name in frames_of(A).get(C, ()):
                    C, A = A, A.parent      # a call-frame temporary of A: not capturable
                    continue
                if d is not None and d.block.func is A:
                    if (A, name) in decisions:
                        claims[(G, tok)] = A
                    break
                if (A, name) in decisions and live_at(decisions[(A, name)][1], C.header_tok):
                    claims[(G, tok)] = A
                    break
                C, A = A, A.parent

    def inside(X, F):
        """X is F or nested in F."""
        while X is not None:
            if X is F:
                return True
            X = X.parent
        return False

    edits = []                          # (start, end, replacement)
    stats = {'pairs': 0, 'renamed': 0, 'converted': 0, 'kept': 0, 'hoisted': 0}
    warnings = []

    def rename_at(ti, new):
        off = toks[ti][3]
        edits.append((off, off + len(toks[ti][1]), new))

    for F, names in leaks.items():
        is_main = F.parent is None
        hoist = []
        for R in sorted(names, key=lambda r: int(r[2:]) if r[2:].isdigit() else 0):
            if not R.startswith('_r'):
                continue
            stats['pairs'] += 1
            decision = decisions[(F, R)][0]
            decls = [d for d in F.decls if d.name == R]
            nested_bound = []
            outward = False
            for G, tok in refs.get(R, ()):
                if not inside(G, F):
                    continue
                cl = claims.get((G, tok))
                if cl is F:
                    nested_bound.append(tok)
                elif cl is None or not (cl is not F and inside(cl, F)):
                    outward = True
            rename = (not is_main) and outward
            new = f'{R}o{depth[F]}' if rename else R
            stats['renamed'] += rename
            handled = set()
            for d in decls:
                first = toks[d.tok][1]
                if first == 'for':
                    reason = 'for-loop variable'
                    j = d.tok + 1
                    name_toks = []
                    while toks[j][1] not in ('in', '='):
                        if toks[j][1] == R:
                            name_toks.append(j)
                        j += 1
                elif toks[d.tok + 1][1] == 'function':
                    reason = 'local function'
                    name_toks = [d.tok + 2]
                else:
                    stmt = _local_names(toks, d.tok)
                    name_toks = [j for j in stmt if toks[j][1] == R]
                    if d.captured_by:
                        reason = 'captured by closure'
                    elif len(stmt) > 1:
                        reason = 'multi-name local'
                    else:
                        reason = None
                if reason is None:
                    nt = name_toks[0]
                    loff = toks[d.tok][3]
                    if toks[nt + 1][1] == '=' and toks[nt + 1][0] == 'op':
                        edits.append((loff, toks[nt][3], ''))
                        if rename:
                            rename_at(nt, new)
                    else:
                        edits.append((loff, toks[nt][3] + len(R), f'{new} = nil'))
                    handled.add(nt)
                    stats['converted'] += 1
                else:
                    stats['kept'] += 1
                    warnings.append(f'fn@{F.line} {R}: kept local at line {d.line} ({reason})')
                    if rename:
                        for nt in name_toks:
                            rename_at(nt, new)
                    handled.update(name_toks)
            if rename:
                for ti, v in decision.items():
                    if v == 'own' and ti not in handled:
                        rename_at(ti, new)
                for ti in nested_bound:
                    rename_at(ti, new)
            if not is_main:
                hoist.append(new)   # main-chunk registers stay globals (main is at the local limit)
        if not hoist:
            continue
        stats['hoisted'] += len(hoist)
        bt = F.body_tok
        boff = toks[bt][3]
        if toks[bt][2] == F.line and not is_main:
            edits.append((boff, boff, f'local {", ".join(hoist)}; '))
        else:
            ls = text.rfind('\n', 0, boff) + 1
            indent = text[ls:boff]
            if indent.strip():
                indent = ''
            edits.append((ls, ls, f'{indent}local {", ".join(hoist)}\n'))
    edits.sort(key=lambda e: (e[0], e[1]))
    for a, b in zip(edits, edits[1:]):
        assert a[1] <= b[0] or (a[0] == a[1] == b[0]), f'overlapping edits {a} {b}'
    out = []
    pos = 0
    for start, end, rep in edits:
        out.append(text[pos:start])
        out.append(rep)
        pos = end
    out.append(text[pos:])
    return ''.join(out), stats, warnings


# --- CLOSED UPVALUES IN THE MAIN CHUNK (Pass 13b) ---
# A closure keeps a captured local after its scope ends (CLOSE); the VM may then reuse the
# register. The main chunk's registers are plain names here, so later reuse (e.g. 30 -> {} ->
# a function) leaks into earlier closures ("arithmetic on number and function").
# A closure is treated as closed when a captured register's next main-chunk write assigns a
# different kind of value than the one it captured; its captured registers then get a new
# name for the [captured write, next write) range.

def _value_kind(rhs):
    rhs = rhs.strip()
    if re.match(r'^-?\d', rhs):
        return 'number'
    if rhs[:1] in ('"', "'") or rhs.startswith('[['):
        return 'string'
    if rhs in ('true', 'false'):
        return 'boolean'
    if rhs == 'nil':
        return 'nil'
    if rhs.startswith('{'):
        return 'table'
    if rhs.startswith('function'):
        return 'function'
    return None


def fix_closed_upvalues(text):
    toks = tokenize(text)
    funcs = analyze(toks)
    main = funcs[0]
    lines = text.split('\n')
    lm = own_line_tokens(main, toks)
    writes = defaultdict(list)                  # R -> [(tok, line, kind)]
    for ln in sorted(lm):
        lt = lm[ln]
        for pos, ti in enumerate(lt):
            if toks[ti][0] == 'name' and REG_NUM_RE.match(toks[ti][1]) and \
                    classify_use(toks, lt, pos) in ('owrite', 'cwrite'):
                src = lines[ln - 1]
                single = re.match(r'^\s*(?:local\s+)?_r\d+\s*=\s*(.*)$', src)
                writes[toks[ti][1]].append((ti, ln, _value_kind(single.group(1)) if single else None))

    def captures(C):
        """{R: [tok]} for main-chunk registers used inside C, plus the set C writes."""
        out = defaultdict(list)
        written = set()
        for G in _subtree(C):
            lg = None
            for name, line, tok, res, d in G.uses:
                if REG_NUM_RE.match(name) and res != 'own' and (d is None or d.block.func is main):
                    out[name].append(tok)
                    lg = lg or own_line_tokens(G, toks)
                    lt = lg.get(line, [])
                    if tok in lt and classify_use(toks, lt, lt.index(tok)) in ('owrite', 'cwrite'):
                        written.add(name)
        return out, written

    def same_block(a, b):
        ind = _indent(lines[b - 1])
        return _indent(lines[a - 1]) == ind and all(
            not lines[q - 1].strip() or _indent(lines[q - 1]) >= ind for q in range(a, b))

    caps = {}
    written_by = {}
    for C in main.children:
        caps[C], written_by[C] = captures(C)
    splits = {}                                  # (R, prev tok) -> (prev, next)
    for C, names in caps.items():
        hline = toks[C.header_tok][2]
        local = {}
        reused = []
        for R in names:
            ws = writes.get(R, [])
            prev = max((w for w in ws if w[0] < C.header_tok), default=None)
            nxt = min((w for w in ws if w[0] > C.end_tok), default=None)
            if prev is None or nxt is None or not same_block(prev[1], hline):
                continue
            # A nil the closure itself fills in is closure state; a nil it only reads is a
            # forward reference to a later main-chunk assignment and stays shared.
            own_state = prev[2] == 'nil' and R in written_by[C]
            if prev[2] == 'nil' and not own_state:
                continue
            local[R] = (prev, nxt)
            if own_state or (prev[2] and nxt[2] and prev[2] != nxt[2]):
                reused.append(int(R[2:]))
        if reused:
            # The closed block's locals sit at or above its lowest register; lower registers
            # are outer locals that stay shared.
            low = min(reused)
            for R, pn in local.items():
                if int(R[2:]) >= low:
                    splits[(R, pn[0][0])] = pn
    edits = []
    for (R, _), (prev, nxt) in splits.items():
        new = f'{R}v{prev[1]}'
        after = lm[prev[1]][-1]                  # the captured write's RHS keeps the old value
        for ln in lm:
            for ti in lm[ln]:
                if toks[ti][1] == R and (ti == prev[0] or after < ti < nxt[0]) \
                        and toks[ti - 1][1] not in ('.', ':'):
                    edits.append((toks[ti][3], toks[ti][3] + len(R), new))
        # Closures created while R was still nil (forward references) see the next write.
        before = max((w for w in writes[R] if w[0] < prev[0]), default=None)
        start = before[0] if before is not None and before[2] == 'nil' else prev[0]
        if before is None:
            start = -1
        for C, names in caps.items():
            if start < C.header_tok < nxt[0]:
                for ti in names.get(R, ()):
                    edits.append((toks[ti][3], toks[ti][3] + len(R), new))
    out = []
    pos = 0
    for start, end, new in sorted(set(edits)):
        out.append(text[pos:start])
        out.append(new)
        pos = end
    out.append(text[pos:])
    return ''.join(out), len(splits)


# --- OPERAND EXTENT ---
# `x = a or b` / `x = a and b` decompile to an empty `if` followed by the statements
# that compute the right operand. These helpers find where that operand ends.

ASSIGN_REG_RE = re.compile(r'^(?:local\s+)?_r(\d+)(?:\s*,\s*_r\d+)*\s*=\s*(.*)$')
MAX_OPERAND = 12


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def block_end(lines, start, ind):
    """Index of the `end` closing the block opened at lines[start] (indent ind).
    Continuation lines of multi-line strings (previous line ends in a backslash) are skipped."""
    j = start + 1
    prev_bs = False
    while j < len(lines):
        ln = lines[j].rstrip('\r\n')
        if not prev_bs:
            if _indent(ln) == ind and ln.strip() == 'end':
                return j
            if _indent(ln) < ind and ln.strip():
                return None
        prev_bs = ln.endswith('\\')
        j += 1
    return None


def operand_extent(lines, start, ind, reg):
    """Return the exclusive end index of the right operand that begins at lines[start].

    Accepts same-indent assignments to registers >= reg, and chained `if [not] reg then`
    blocks whose first body line assigns reg. Trimmed to the last line that assigns reg.
    Returns start when no operand is found.
    """
    xn = int(reg[2:])
    pat_x = re.compile(r'^(?:local\s+)?' + re.escape(reg) + r'(?:\s*,\s*_r\d+)*\s*=')
    pat_if = re.compile(r'^if (?:not )?' + re.escape(reg) + r' then$')
    j = start
    last = start
    while j < len(lines) and j - start < MAX_OPERAND:
        ln = lines[j].rstrip('\r\n')
        if _indent(ln) != ind:
            break
        s = ln.strip()
        if pat_if.match(s):
            e = block_end(lines, j, ind)
            if e is None or e == j + 1:
                break          # empty if: a separate chained test, handled on its own
            if not pat_x.match(lines[j + 1].strip()):
                break          # consumer, not chain continuation
            j = e + 1
            last = j
            continue
        m = ASSIGN_REG_RE.match(s)
        if not m or int(m.group(1)) < xn or 'function' in m.group(2):
            break
        j += 1
        if pat_x.match(s):
            last = j
    return last


# --- TESTSET RESTORATION (Pass 0) ---
# A copy from the function's OWN register/param followed by a test (`X = Y` /
# `if [not] X then`) is a TESTSET. Unlike TEST, the decompiler prints TESTSET
# conditions with the correct sense, so Pass 1 must not flip them. Copies from true
# upvalues (GETUPVAL + TEST) are inverted like every other TEST.

TS_MARK = ' --[[TS]]'
COPY_RE = re.compile(r'^(\s*)(?:local\s+)?(_r\d+) = (_r\d+|_p\d+)\s*$')
TEST_RE = re.compile(r'^(\s*)if (not )?(_r\d+) then\s*$')


def _is_own_source(F, Y, tok, res, toks, cache):
    """True when Y is F's own register/param in VM terms (TESTSET), False for an upvalue."""
    if res == 'own':
        return True
    if Y.startswith('_p') or not any(d.name == Y and d.tok < tok for d in F.decls):
        return False
    key = (id(F), Y)
    if key not in cache:
        cache[key] = classify_pair(F, Y, toks, own_line_tokens(F, toks), F.parent is None)[0]
    return cache[key].get(tok) == 'own'


def fix_testsets(lines):
    """Mark TESTSET tests so Pass 1 leaves them, and move empty-if right operands inside."""
    text = ''.join(lines)
    toks = tokenize(text)
    funcs = analyze(toks)
    by_line = defaultdict(list)
    for idx, t in enumerate(toks):
        by_line[t[2]].append(idx)
    use_info = {}
    for F in funcs:
        for _, _, tok, res, _ in F.uses:
            use_info[tok] = (F, res)
    cache = {}
    targets = []
    for i in range(1, len(lines)):
        f = TEST_RE.match(lines[i].rstrip('\r\n'))
        if not f:
            continue
        m = COPY_RE.match(lines[i - 1].rstrip('\r\n'))
        if not m or m.group(2) != f.group(3) or m.group(1) != f.group(1):
            continue
        Y = m.group(3)
        ytoks = [t for t in by_line[i] if toks[t][1] == Y]
        if not ytoks or ytoks[-1] not in use_info:
            continue
        F, res = use_info[ytoks[-1]]
        if _is_own_source(F, Y, ytoks[-1], res, toks, cache):
            targets.append(i)

    stats = {'marked': 0, 'operand_moved': 0, 'ternary_else': 0}
    for i in reversed(targets):
        s = lines[i].rstrip('\r\n')
        ending = lines[i][len(s):]
        lines[i] = s + TS_MARK + ending
        stats['marked'] += 1
        ind = len(s) - len(s.lstrip(' '))
        reg = TEST_RE.match(s).group(3)
        if lines[i + 1].rstrip('\r\n') != ' ' * ind + 'end':
            continue
        start = i + 2
        stop = operand_extent(lines, start, ind, reg)
        if stop == start:
            continue
        operand = lines[start:stop]
        new = ['    ' + ln for ln in operand] + [lines[i + 1]]
        parent = lines[i - 2].rstrip('\r\n') if i >= 2 else ''
        if (stop < len(lines) and ind >= 4
                and lines[stop].rstrip('\r\n') == ' ' * (ind - 4) + 'end'
                and len(parent) - len(parent.lstrip(' ')) == ind - 4
                and re.match(r'^\s*if\b.*\b' + re.escape(reg) + r'\b.*\bthen$', parent)):
            new += [' ' * (ind - 4) + 'else' + ending] + operand
            stats['ternary_else'] += 1
        lines[i + 1:stop] = new
        stats['operand_moved'] += 1
    return lines, stats


# --- STRUCTURAL REPAIRS (Pass 0b) ---
# One-off block-structure errors on the raw decompiler output. In each case the decompiler
# lost a block boundary and left the block open until the end of the file, nesting the rest
# of the script inside it. Every repair is anchored on exact text and asserted.

def _dedent(lines, lo, hi, n=4):
    """Remove n leading spaces from lines[lo:hi], skipping multi-line string continuations."""
    pad = ' ' * n
    prev_bs = lo > 0 and lines[lo - 1].rstrip('\r\n').endswith('\\')
    for k in range(lo, hi):
        if not prev_bs and lines[k].startswith(pad):
            lines[k] = lines[k][n:]
        prev_bs = lines[k].rstrip('\r\n').endswith('\\')


def _find_line(lines, pattern, start=0):
    rx = re.compile(pattern)
    for k in range(start, len(lines)):
        if rx.match(lines[k].rstrip('\r\n')):
            return k
    return None


# (c) Game-ready check: `if menu and (visible(menu) or (modes and visible(modes))) then
# return false end`. The or-chain decompiles with its return outside both tests. Written in
# raw (pre-Pass 1) polarity.
GAMEMODES_OR_RE = re.compile(
    r'(?P<i>[ ]*)if not _r6 then\n'
    r'(?P=i)    _r8 = _r62\n(?P=i)    _r9 = _r6\n(?P=i)    _r8 = _r8\(_r9\)\n'
    r'(?P=i)    if _r8 then\n(?P=i)    end\n'
    r'(?P=i)    if not _r7 then\n'
    r'(?P=i)        _r8 = _r62\n(?P=i)        _r9 = _r7\n(?P=i)        _r8 = _r8\(_r9\)\n'
    r'(?P=i)        if not _r8 then\n(?P=i)        end\n'
    r'(?P=i)        _r8 = false\n(?P=i)        do return _r8 end\n'
    r'(?P=i)    end\n'
    r'(?P=i)end\n'
)


def _gamemodes_or_repl(m):
    i = m.group('i')
    return (
        f'{i}if not _r6 then\n'
        f'{i}    _r8 = _r62\n{i}    _r9 = _r6\n{i}    _r8 = _r8(_r9)\n'
        f'{i}    if _r8 then\n'
        f'{i}        if not _r7 then\n'
        f'{i}            _r8 = _r62\n{i}            _r9 = _r7\n{i}            _r8 = _r8(_r9)\n'
        f'{i}        end\n'
        f'{i}    end\n'
        f'{i}    if not _r8 then\n'
        f'{i}        _r8 = false\n{i}        do return _r8 end\n'
        f'{i}    end\n'
        f'{i}end\n'
    )


def fix_structural_repairs(lines):
    lines = list(lines)
    stats = {}

    # (a) Reverse loop removing non-farmable items from selectedFarmItems: its `end` belongs
    # before the `if _r172 then` save that follows the loop.
    f = _find_line(lines, r'^(\s*)for _r176 = _r173, _r174, _r175 do$')
    assert f is not None, 'repair (a): loop header not found'
    ind = len(lines[f]) - len(lines[f].lstrip(' '))
    a = _find_line(lines, r'^' + ' ' * (ind + 4) + r'if not _r172 then$', f + 1)
    e = block_end(lines, f, ind)
    assert a is not None and e is not None and a < e, 'repair (a): anchors not found'
    _dedent(lines, a, e)
    del lines[e]
    lines.insert(a, ' ' * ind + 'end\n')
    stats['loop_end'] = 1

    # (b) Anti-teleport bypass: the failure branch is the lost `else` of the pcall check,
    # and the code after it (UI header image onward) runs either way.
    h = _find_line(lines, r'^\s*if not _r148 then$')
    assert h is not None and lines[h - 1].strip() == '_r148, _r149 = _r148(_r149)', 'repair (b): header'
    hind = len(lines[h]) - len(lines[h].lstrip(' '))
    fl = _find_line(lines, r'^\s*_r152 = "Bypass failed - using Tween movement to avoid server kick"$', h)
    f0 = fl - 2
    assert lines[f0].strip() == '_r150 = _r110', 'repair (b): failure branch start'
    g = f0
    while lines[g].strip() != 'end' or len(lines[g]) - len(lines[g].lstrip(' ')) != hind + 4:
        g += 1
    e = block_end(lines, h, hind)
    assert e is not None and g < e, 'repair (b): block end'
    _dedent(lines, g + 1, e)
    del lines[e]
    lines[g] = ' ' * hind + 'end\n'
    _dedent(lines, f0, g)
    lines[f0:f0] = [' ' * (hind + 4) + 'end\n', ' ' * hind + 'else\n']
    stats['bypass_else'] = 1

    # (c) Game-ready GameModes or-chain.
    text = ''.join(lines)
    text, n = GAMEMODES_OR_RE.subn(_gamemodes_or_repl, text)
    assert n == 1, f'repair (c): expected 1 match, got {n}'
    lines = split_lines(text)
    stats['gamemodes_or'] = n

    # (d) The game-ready wait is `while true do / while true do ...`: the outer loop is a
    # decompiler artifact that wraps the whole rest of the script. Remove it.
    w = _find_line(lines, r'^\s*_r100 = "Waiting for strict main game before initializing YBAHub UI & features\.\.\."$')
    assert w is not None, 'repair (d): anchor'
    o = _find_line(lines, r'^\s*while true do$', w)
    assert o is not None and o - w < 8 and lines[o + 1].strip() == 'while true do', 'repair (d): nested loops'
    oind = len(lines[o]) - len(lines[o].lstrip(' '))
    e = block_end(lines, o, oind)
    assert e is not None, 'repair (d): loop end'
    _dedent(lines, o + 1, e)
    del lines[e]
    del lines[o]
    stats['outer_while'] = 1

    return lines, stats


# --- BOOLEAN MATERIALIZATION (Pass 1b) ---
# `x = (a == b)` / `x = (v ~= false)` compile to a compare plus two boolean loads. The
# decompiler keeps only the first load, drops `else x = <other>`, and nests the whole
# continuation inside the if:
#   if a ~= b then / x = false / <rest of the enclosing block> / end
# Detected when the block runs to the end of its enclosing block (nothing follows it).
# Fix: if a ~= b then / x = false / else / x = true / end / <rest, dedented>

CMP_IF_RE = re.compile(r'^(\s*)if (.+ (?:==|~=|<=|>=|<|>) .+) then$')
BOOL_LOAD_RE = re.compile(r'^(\s*)(_r\d+) = (true|false)$')
# `if type(x) == "function" then flag = true ...` is a guard whose body needs the condition.
TYPE_GUARD_RE = re.compile(r'[=~]= "(?:nil|boolean|number|string|table|function|userdata|thread|Instance)"')


def fix_bool_materialization(lines):
    lines = list(lines)
    count = 0
    i = 0
    while i < len(lines) - 2:
        m = CMP_IF_RE.match(lines[i].rstrip('\r\n'))
        b = BOOL_LOAD_RE.match(lines[i + 1].rstrip('\r\n')) if m else None
        if not b:
            i += 1
            continue
        ind = len(m.group(1))
        X, val = b.group(2), b.group(3)
        if len(b.group(1)) != ind + 4 or not re.search(r'\b' + re.escape(X) + r'\b', m.group(2)) \
                or TYPE_GUARD_RE.search(m.group(2)):
            i += 1
            continue
        e = block_end(lines, i, ind)
        if e is None or e - i <= 2 or any(
                lines[k].startswith(' ' * ind + 'else') and _indent(lines[k]) == ind for k in range(i + 1, e)):
            i += 1
            continue
        k = e + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k < len(lines) and _indent(lines[k]) >= ind:
            i += 1
            continue
        if re.match(r'^(?:do )?return\b', lines[i + 2].strip()):
            # `return a ~= b` at the end of a function, or a guarded `return false` at the
            # end of an inner block? Only the former when the enclosing block is a function.
            o = i - 1
            while o >= 0 and (not lines[o].strip() or _indent(lines[o]) >= ind):
                o -= 1
            if o < 0 or 'function(' not in lines[o]:
                i += 1
                continue
        pad = ' ' * ind
        opp = 'true' if val == 'false' else 'false'
        _dedent(lines, i + 2, e)
        lines[e:e + 1] = []
        lines[i + 2:i + 2] = [pad + 'else\n', pad + f'    {X} = {opp}\n', pad + 'end\n']
        count += 1
        i += 2
    return lines, count


# --- OR-CHAINS (Pass 3b) ---
# `if a or b then REST end` (and longer chains) decompiles to nested tests whose innermost
# test is empty, with REST left at the end of the outermost block:
#   if F1 then / <setup> / [if F2 then / <setup> / ...] if C then / end / [end ...] / REST / end
# where F1, F2 (after Pass 1) are the "keep evaluating" conditions and C the final one.
# Real meaning: REST runs iff (not F1) or (not F2) or ... or C. As decompiled it runs iff F1.
# Fix with a flag:
#   local _orK = true / if F1 then <setup> [...] if not (C) then _orK = false end [...] end
#   if _orK then / REST / end
# Applied only when every test in the chain shares a register, setups are plain
# assignments, no level has an else, and REST is non-empty.

COND_IF_RE = re.compile(r'^(\s*)if (.+) then$')
COND_REG_RE = re.compile(r'\b_[rp]\d+\b')


def _negate_cond(cond):
    m = re.match(r'^not (_[rp]\d+)$', cond)
    if m:
        return m.group(1)
    if re.match(r'^_[rp]\d+$', cond):
        return 'not ' + cond
    return f'not ({cond})'


def _has_else(lines, lo, hi, ind):
    pad = ' ' * ind
    return any(lines[k].startswith(pad + 'else') and _indent(lines[k]) == ind for k in range(lo, hi))


def _chain_level(lines, start, end, ind, regs, relaxed=False):
    """Scan the body of the if at lines[start] (indent ind, closed at end) for the chain's
    next test: plain-assignment setup lines, then an `if` sharing a register with regs
    (relaxed: or testing a register the setup assigned).
    Returns (if_index, its block end, regs for the next level) or None."""
    body = ind + 4
    k = start + 1
    assigned = set()
    while k < end:
        s = lines[k].rstrip('\r\n')
        if _indent(s) == body and COND_IF_RE.match(s):
            break
        if _indent(s) != body or not SIMPLE_STMT_RE.match(s.strip()) or 'function' in s:
            return None
        assigned.update(n.strip() for n in s.split('=')[0].replace('local ', '').split(','))
        k += 1
    if k >= end:
        return None
    nregs = set(COND_REG_RE.findall(COND_IF_RE.match(lines[k].rstrip('\r\n')).group(2)))
    shared = nregs & (regs | assigned) if relaxed else nregs & regs
    ek = block_end(lines, k, body)
    if not shared or ek is None or _has_else(lines, k + 1, ek, body):
        return None
    return k, ek, (nregs if relaxed else shared)


def _find_chain(lines, i, I, e, regs, relaxed):
    """Descend the chain of the if at lines[i]; return (empty test index, end of the
    depth-1 element, depth of the empty test) or None."""
    lvl_start, lvl_ind, lvl_end = i, I, e
    top_end = None
    while True:
        nxt = _chain_level(lines, lvl_start, lvl_end, lvl_ind, regs, relaxed)
        if nxt is None:
            return None
        k, ek, regs = nxt
        if lvl_start != i and any(lines[q].strip() for q in range(ek + 1, lvl_end)):
            return None                    # deeper levels must end with the next test
        if lvl_start == i:
            top_end = ek
        if ek == k + 1:
            return k, top_end, (lvl_ind - I) // 4 + 1
        lvl_start, lvl_ind, lvl_end = k, lvl_ind + 4, ek


def _relaxed_rest_ok(rest, test_line):
    """With registers that differ per level, REST inside the outer block is accepted unless
    it starts by assigning the final test's register (Pass 4's `x = x or v` operand), with
    the exception of a plain `X = <literal>` / `return X` block."""
    m = re.search(r'\b(_[rp]\d+)\b', COND_IF_RE.match(test_line.rstrip('\r\n')).group(2))
    first = rest[0].strip()
    if not m or not re.match(r'^(?:local\s+)?' + re.escape(m.group(1)) + r'\s*=', first):
        return True
    return len(rest) == 2 and re.match(r'^(?:do )?return\b', rest[1].strip()) is not None \
        and re.match(r'^(?:local\s+)?_r\d+\s*=\s*(?:true|false|nil|-?\d[\d.]*|"[^"]*")$', first) is not None


def _dropped_rest_end(lines, e, I):
    """REST dropped after the outer block: a short run of plain statements ending in a
    return (a body reached from several tests cannot be nested by the decompiler).
    Returns the index of the return line, or None."""
    r = e + 1
    while r < len(lines) and r - e <= 4 and _indent(lines[r]) == I \
            and SIMPLE_STMT_RE.match(lines[r].strip()) and 'function' not in lines[r]:
        r += 1
    if r >= len(lines) or r - e > 4 or _indent(lines[r]) != I \
            or not re.match(r'^(?:do )?return\b', lines[r].strip()):
        return None
    return r


def fix_or_chains(lines):
    lines = list(lines)
    count = 0
    i = 0
    while i < len(lines):
        m = COND_IF_RE.match(lines[i].rstrip('\r\n'))
        regs = set(COND_REG_RE.findall(m.group(2))) if m else set()
        if not regs:
            i += 1
            continue
        I = len(m.group(1))
        e = block_end(lines, i, I)
        if e is None or _has_else(lines, i + 1, e, I):
            i += 1
            continue
        rest = None
        for relaxed in (False, True):
            found = _find_chain(lines, i, I, e, regs, relaxed)
            if found is None:
                continue
            E, top_end, depth = found
            if any(lines[q].strip() for q in range(top_end + 1, e)):
                inside = [ln for ln in lines[top_end + 1:e] if ln.strip()]
                if relaxed and depth < 2 and not _relaxed_rest_ok(inside, lines[E]):
                    continue
                rest = lines[top_end + 1:e]      # REST inside the outer block
                tail_end = e + 1
                break
            r = _dropped_rest_end(lines, e, I)
            if r is not None:
                rest = lines[e + 1:r + 1]
                tail_end = r + 1
                break
        if rest is None:
            i += 1
            continue
        pad = ' ' * I
        eind = _indent(lines[E])
        flag = f'_or{count}'
        cond = COND_IF_RE.match(lines[E].rstrip('\r\n')).group(2)
        new = [pad + f'local {flag} = true\n']
        new += lines[i:E]
        new += [' ' * eind + f'if {_negate_cond(cond)} then\n', ' ' * (eind + 4) + f'{flag} = false\n']
        new += lines[E + 1:top_end + 1]
        new += [lines[e], pad + f'if {flag} then\n']
        new += ['    ' + ln for ln in rest] if tail_end > e + 1 else rest
        new += [pad + 'end\n']
        lines[i:tail_end] = new
        count += 1
        i += 1
    if count:
        # Main-chunk registers are globals and the main chunk is at the 200-local limit,
        # so flags declared in the main chunk's own scope become globals as well.
        main = analyze(tokenize(''.join(lines)))[0]
        for d in main.decls:
            if d.name.startswith('_or'):
                ln = d.line - 1
                lines[ln] = lines[ln].replace(f'local {d.name} = ', f'{d.name} = ', 1)
    return lines, count


# --- WHILE-CONDITION CHAINS (Pass 8b) ---
# `while A and B and C do BODY end` decompiles to a `while true do` whose whole body is a
# chain of nested ifs with every exit dropped:
#   while true do / if A then / <setup> / if B then / <setup> / if C then / BODY / end / end / end / end
# Each level is plain setup statements followed by the next test (testing a register the
# setup computed) as its last statement. When a test fails the loop must exit, so every
# if in the chain gets `else break`. Descent stops at the first level that does not fit.

def _while_chain(lines, k, ind, first_end=None):
    """Indices of the ifs forming a while-condition chain starting at lines[k]. first_end,
    when given, is the line closing the first level's then-branch (its `else`)."""
    chain = [k]
    while True:
        e = block_end(lines, k, ind) if first_end is None or len(chain) > 1 else first_end
        body = ind + 4
        q = k + 1
        assigned = set()
        while q < e and _indent(lines[q]) == body and SIMPLE_STMT_RE.match(lines[q].strip()) \
                and 'function' not in lines[q]:
            assigned.update(n.strip() for n in lines[q].strip().split('=')[0].replace('local ', '').split(','))
            q += 1
        m = COND_IF_RE.match(lines[q].rstrip('\r\n')) if q < e and _indent(lines[q]) == body else None
        if not m or not assigned & set(COND_REG_RE.findall(m.group(2))):
            return chain
        eq = block_end(lines, q, body)
        if eq != e - 1 or _has_else(lines, q + 1, eq, body):
            return chain
        chain.append(q)
        k, ind = q, body


def fix_while_chains(lines):
    lines = list(lines)
    count = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() != 'while true do':
            continue
        w = _indent(lines[i])
        e = block_end(lines, i, w)
        k = i + 1
        if e is None or k >= e:
            continue
        assigned = set()
        while k < e and _indent(lines[k]) == w + 4 and SIMPLE_STMT_RE.match(lines[k].strip()) \
                and 'function' not in lines[k]:
            assigned.update(n.strip() for n in lines[k].strip().split('=')[0].replace('local ', '').split(','))
            k += 1
        m = COND_IF_RE.match(lines[k].rstrip('\r\n')) if k < e and _indent(lines[k]) == w + 4 else None
        if not m or block_end(lines, k, w + 4) != e - 1:
            continue
        if k > i + 1 and not assigned & set(COND_REG_RE.findall(m.group(2))):
            continue                     # setup must compute the loop condition
        pad = ' ' * (w + 4)
        elses = [q for q in range(k + 1, e - 1) if _indent(lines[q]) == w + 4
                 and lines[q].strip().startswith('else')]
        if elses:
            # `while A do BODY end; AFTER` rendered as `if A then BODY else AFTER end` in the
            # loop: AFTER (no yield) belongs after the loop, the failing test exits.
            if len(elses) != 1 or lines[elses[0]].strip() != 'else':
                continue
            after = lines[elses[0] + 1:e - 1]
            if any(YIELD_RE.search(ln) for ln in after):
                continue
            chain = _while_chain(lines, k, w + 4, first_end=elses[0])
            if [ln.strip() for ln in after if ln.strip()] != ['break']:
                moved = list(after)
                _dedent(moved, 0, len(moved), 8)
                lines[elses[0] + 1:e - 1] = [pad + '    break\n']
                e = block_end(lines, i, w)
                lines[e + 1:e + 1] = moved
            elif len(chain) < 2:
                continue                 # already `while A do`, nothing deeper to fix
            chain = chain[1:]
        else:
            chain = _while_chain(lines, k, w + 4)
            if len(chain) < 2 and k == i + 1 and IF_GUARD_RE.match(lines[k].rstrip('\r\n')):
                continue                 # single bare test: Pass 9 adds its guard break
        for c in reversed(chain):
            ci = _indent(lines[c])
            ce = block_end(lines, c, ci)
            lines[ce:ce] = [' ' * ci + 'else\n', ' ' * (ci + 4) + 'break\n']
        count += 1
    return lines, count


# --- LOOP TAIL EXITS (Pass 8c) ---
# Search loops `repeat <find>; if <found> then <success>; break end; task.wait() until` /
# `while true do ... if A and B then ok = true; break end; task.wait(x) end` decompile with
# the exit dropped and the trailing wait placed in the else of the FIRST test only:
#   if A then / if B then / ok = true / end / else / task.wait(x) / end   (in loop tail)
# so any inner failure, or success, loops again without yielding (a client freeze).
# Fix: (a) the yielding else-content runs after the if; (b) when the then-branch only does
# lookups (no calls that could yield or act), break at its innermost success point.

YIELD_RE = re.compile(r'\.wait\b|\bwait\b|\.Wait\b')
SAFE_CALLS = {
    'FindFirstChild', 'FindFirstChildOfClass', 'FindFirstChildWhichIsA', 'FindFirstAncestor',
    'FindFirstAncestorOfClass', 'IsA', 'IsDescendantOf', 'GetChildren', 'GetDescendants',
    'GetAttribute', 'tostring', 'tonumber', 'type', 'typeof', 'find', 'lower', 'upper', 'sub',
    'match', 'gsub', 'format', 'len', 'tick', 'time', 'clock', '_r20',
}


def _branch_pure(seg):
    """True when every call in seg goes to a lookup/string helper or the boot logger."""
    callee = {}
    for ln in seg:
        s = ln.strip()
        for c in re.findall(r'(_[rp]\w+)\(', s):
            if callee.get(c) not in SAFE_CALLS:
                return False
        m = re.match(r'^(?:local\s+)?(_r\w+) = ([\w.]+)$', s)
        if m:
            callee[m.group(1)] = m.group(2).split('.')[-1]
    return True


def _innermost_body_end(lines, o, ind):
    """Follow the last statement of the if at lines[o] while it is an else-less if;
    return the index of the innermost block's `end`."""
    while True:
        e = block_end(lines, o, ind)
        q = e - 1
        while q > o and not lines[q].strip():
            q -= 1
        if _indent(lines[q]) != ind + 4 or lines[q].strip() != 'end':
            return e
        p = q - 1
        while p > o and not (_indent(lines[p]) == ind + 4 and lines[p].strip().startswith('if ')):
            p -= 1
        if p <= o or block_end(lines, p, ind + 4) != q or _has_else(lines, p + 1, q, ind + 4):
            return e
        o, ind = p, ind + 4


def fix_loop_tail_exits(lines):
    lines = list(lines)
    stats = {'yield_moved': 0, 'success_break': 0}
    i = len(lines) - 1
    while i >= 0:
        if lines[i].strip() != 'while true do':
            i -= 1
            continue
        w = _indent(lines[i])
        e = block_end(lines, i, w)
        o = e - 1 if e else i
        while e and o > i:
            s = lines[o].strip()
            ind = _indent(lines[o])
            if s.startswith('if ') and s.endswith(' then'):
                k = block_end(lines, o, ind)
                tail = k is not None and k < e and all(
                    lines[q].strip() in ('end', 'else', 'break', '') for q in range(k + 1, e))
                elses = [q for q in range(o + 1, k) if _indent(lines[q]) == ind
                         and lines[q].strip().startswith('else')] if tail else []
                in_fn = any('function(' in lines[q] and _indent(lines[q]) < ind for q in range(i + 1, o))
                if tail and len(elses) == 1 and lines[elses[0]].strip() == 'else' and not in_fn:
                    a, b = lines[o + 1:elses[0]], lines[elses[0] + 1:k]
                    a_exits = any(ln.strip() in ('break',) or ln.strip().startswith(('return', 'do return'))
                                  for ln in a)
                    if any(YIELD_RE.search(ln) for ln in b) and not any(YIELD_RE.search(ln) for ln in a):
                        brk = None
                        if not a_exits and _branch_pure(a):
                            if any(ln.strip().startswith('if ') for ln in a):
                                p = o + 1
                                last_if = max(q for q in range(o + 1, elses[0])
                                              if _indent(lines[q]) == ind + 4 and lines[q].strip().startswith('if '))
                                if block_end(lines, last_if, ind + 4) == elses[0] - 1:
                                    brk = _innermost_body_end(lines, last_if, ind + 4)
                            else:
                                brk = elses[0]
                        lines[elses[0]:k + 1] = [lines[k]] + b   # else-content after the if
                        _dedent(lines, elses[0] + 1, elses[0] + 1 + len(b))
                        stats['yield_moved'] += 1
                        if brk is not None:
                            bi = _indent(lines[brk])
                            lines.insert(brk, ' ' * (bi + 4 if brk != elses[0] else ind + 4) + 'break\n')
                            stats['success_break'] += 1
                        break
            o -= 1
        i -= 1
    return lines, stats


# --- MULTI-RETURN FORWARDING (Pass 2b) ---
# `f(g(...))` (all results of g forwarded) decompiles to a fixed register range:
#   _rX = g(...) --[[multi-ret]]
#   f(..., _rX, _rX+1, ..., _rN)
# The extra registers hold stale values, e.g. tonumber(v, "IESPSize") with a string base.
# Fix: inline the call as the last argument: f(..., g(...)).

MULTIRET_RE = re.compile(r'^(\s*)(?:local\s+)?_r(\d+) = ((?:_r\d+|\w+)\(.*\)) --\[\[multi-ret\]\]\s*$')


def fix_multiret(lines):
    lines = list(lines)
    count = 0
    i = 0
    while i < len(lines) - 1:
        m = MULTIRET_RE.match(lines[i].rstrip('\r\n'))
        if not m:
            i += 1
            continue
        x = int(m.group(2))
        nxt = lines[i + 1]
        t = re.search(r'(?<=[( ])(_r%d(?:, _r\d+)*)\)' % x, nxt)
        if not t or [int(r[2:]) for r in t.group(1).split(', ')] != \
                list(range(x, x + t.group(1).count(',') + 1)):
            i += 1
            continue
        lines[i + 1] = nxt[:t.start(1)] + m.group(3) + nxt[t.end(1):]
        del lines[i]
        count += 1
    return lines, count


# --- GARBLED EMPTY STRINGS (Pass 14) ---
# The empty string "" decodes to a 2-byte garbage constant (a different one per function),
# e.g. gsub("^%s+", "[\xbc") for a trim, or `if url ~= "\xc7\x84" then`. Every 2-byte
# constant that is not valid text is one of these. A few garbled values happen to be
# printable; those were identified from context (emptiness checks, `or ""` defaults,
# field resets) and are listed explicitly. Legitimate 2-char strings ("SF", "RM", "p6",
# "%D", ...) are not touched.

GARBLED_PRINTABLE = {s.encode('utf-8') for s in (
    '}U', '(&', '|1', 'DX', '[-', 'pV', '4L', '\u01c4', 're', 'Y(', '\u0789', 'ax', 'gF',
    '\u0397', '2@',
)}
LUA_ESCAPES = {'n': 10, 't': 9, 'r': 13, 'a': 7, 'b': 8, 'f': 12, 'v': 11,
               '\\': 92, '"': 34, "'": 39, '\n': 10}


def _lua_string_bytes(body):
    """Decode the body of a quoted Lua string literal (latin-1 text) to bytes."""
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != '\\':
            out.append(ord(ch))
            i += 1
            continue
        m = re.match(r'\\(\d{1,3})', body[i:])
        if m:
            out.append(int(m.group(1)) & 0xFF)
            i += len(m.group(0))
            continue
        out.append(LUA_ESCAPES.get(body[i + 1], ord(body[i + 1])))
        i += 2
    return bytes(out)


def _is_garbled_empty(b):
    if len(b) != 2:
        return False
    if b in GARBLED_PRINTABLE:
        return True
    try:
        txt = b.decode('utf-8')
    except UnicodeDecodeError:
        return True
    return any(ord(c) < 32 for c in txt)


def fix_garbled_strings(text):
    """Replace garbled 2-byte empty-string constants with ""."""
    out = []
    pos = 0
    count = 0
    for m in TOKEN_RE.finditer(text):
        if m.lastgroup != 'string':
            continue
        lit = m.group(0)
        if _is_garbled_empty(_lua_string_bytes(lit[1:-1])):
            out.append(text[pos:m.start()])
            out.append(lit[0] * 2)
            pos = m.end()
            count += 1
    out.append(text[pos:])
    return ''.join(out), count


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else 'yba_hub_deobfuscated.lua'
    output_file = sys.argv[2] if len(sys.argv) > 2 else 'yba_fixed.lua'

    with open(input_file, 'r', encoding='latin-1') as f:
        lines = f.readlines()

    print(f"Read {len(lines)} lines from {input_file}")

    # Pass 0b: Structural repairs on the raw output (blocks left open to end of file)
    lines, sr_stats = fix_structural_repairs(lines)
    sr_total = sum(sr_stats.values())
    print(f"Pass 0b: {sr_total} structural repairs: " + ', '.join(f'{k}={v}' for k, v in sr_stats.items()))

    # Pass 0: TESTSET restoration (marks tests Pass 1 must not flip)
    lines, ts_stats = fix_testsets(lines)
    ts_total = ts_stats['marked'] + ts_stats['operand_moved'] + ts_stats['ternary_else']
    print(f"Pass 0: Marked {ts_stats['marked']} TESTSET tests, moved {ts_stats['operand_moved']} "
          f"right operands, restored {ts_stats['ternary_else']} ternary else-branches")

    # Pass 1: Fix inversions
    inv_count = 0
    fixed_lines = []
    for line in lines:
        new_line = fix_inversion(line)
        if new_line != line:
            inv_count += 1
        fixed_lines.append(new_line)

    print(f"Pass 1: Fixed {inv_count} truthiness inversions")

    # Pass 1b: Boolean materializations with nested continuations
    fixed_lines, bm_count = fix_bool_materialization(fixed_lines)
    print(f"Pass 1b: Fixed {bm_count} boolean materializations")

    # Pass 2: Fix setlists
    fixed_lines, sl_stats = fix_setlists(fixed_lines)

    print(f"Pass 2: Fixed {sl_stats['fixed']} setlist bugs, skipped {sl_stats['skipped']}")

    # Pass 2b: Multi-return forwarding
    fixed_lines, mr_count = fix_multiret(fixed_lines)
    print(f"Pass 2b: Fixed {mr_count} multi-return forwarding calls")

    # Pass 3: Targeted patches (operate on full text, not line-by-line)
    full_text = ''.join(fixed_lines)
    full_text, tp_stats = apply_targeted_patches(full_text)
    fixed_lines = split_lines(full_text)

    tp_total = sum(tp_stats.values())
    print(f"Pass 3: {tp_total} targeted patches applied:")
    for name, count in tp_stats.items():
        print(f"  - {name}: {count}")

    # Pass 3b: Or-chains (must run before Pass 4 and Pass 11 read the empty ifs)
    fixed_lines, oc_count = fix_or_chains(fixed_lines)
    print(f"Pass 3b: Fixed {oc_count} or-chain conditions")

    # Pass 4: Fix broken if/else reconstruction
    fixed_lines, ie_count = fix_broken_if_else(fixed_lines)
    print(f"Pass 4: Fixed {ie_count} broken if/else blocks")

    # Pass 5: Fix varargs passthrough wrappers
    fixed_lines, va_count = fix_varargs_passthrough(fixed_lines)
    print(f"Pass 5: Fixed {va_count} varargs passthrough wrappers")

    # Pass 6: Fix timeout breaks
    full_text = ''.join(fixed_lines)
    full_text, tb_count = fix_timeout_breaks(full_text)
    fixed_lines = split_lines(full_text)
    print(f"Pass 6: Fixed {tb_count} timeout break statements")

    # Pass 7: Branding
    full_text = ''.join(fixed_lines)
    full_text, br_count = fix_branding(full_text)
    fixed_lines = split_lines(full_text)
    print(f"Pass 7: Fixed {br_count} branding strings")

    # Pass 8: Key system stubs
    full_text = ''.join(fixed_lines)
    full_text, ks_count = fix_key_stubs(full_text)
    fixed_lines = split_lines(full_text)
    print(f"Pass 8: Replaced {ks_count} key system function(s) with stubs")

    # Pass 8b: While-condition chains (must run before Pass 9 adds guards/yields)
    fixed_lines, wc_count = fix_while_chains(fixed_lines)
    print(f"Pass 8b: Fixed {wc_count} while-condition chains")

    # Pass 8c: Loop tail exits (search loops with the exit dropped)
    fixed_lines, lt_stats = fix_loop_tail_exits(fixed_lines)
    print(f"Pass 8c: Moved {lt_stats['yield_moved']} loop-tail yields, added {lt_stats['success_break']} success breaks")

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
    fixed_lines = split_lines(full_text)
    print(f"Pass 12: Fixed {gis_count} game-ready initial skip")

    # Pass 13: Scope leaks (must run last among structural passes)
    full_text = ''.join(fixed_lines)
    full_text, sc_stats, sc_warnings = fix_scope_leaks(full_text)
    print(f"Pass 13: Fixed {sc_stats['pairs']} leaked registers ({sc_stats['converted']} declarations "
          f"widened, {sc_stats['renamed']} renamed for upvalue collisions, {sc_stats['kept']} kept local)")

    # Pass 13b: Closed upvalues in the main chunk (registers reused after a closure's scope)
    full_text, cu_count = fix_closed_upvalues(full_text)
    print(f"Pass 13b: Split {cu_count} reused main-chunk registers captured by closures")

    # Pass 14: Garbled empty strings
    full_text, gs_count = fix_garbled_strings(full_text)
    print(f"Pass 14: Replaced {gs_count} garbled empty-string constants")

    full_text = full_text.replace(TS_MARK, '')
    fixed_lines = split_lines(full_text)

    with open(output_file, 'w', encoding='latin-1') as f:
        f.writelines(fixed_lines)

    total = sr_total + ts_total + inv_count + bm_count + sl_stats['fixed'] + mr_count + oc_count + wc_count + lt_stats['yield_moved'] + lt_stats['success_break'] + tp_total + ie_count + va_count + tb_count + br_count + ks_count + gb_count + sy_count + pu_count + dr_count + gis_count + sc_stats['pairs'] + cu_count + gs_count
    print(f"\nTotal: {total} fixes applied")
    print(f"Output written to {output_file} ({len(fixed_lines)} lines)")


if __name__ == '__main__':
    main()
