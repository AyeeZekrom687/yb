#!/usr/bin/env python3
"""
Fix decompiler bugs in YBA deobfuscated Lua script.

Passes run in this order:
0b. Structural repairs: four one-off blocks the decompiler left open to the end of the file
    (selectedFarmItems cleanup loop, anti-teleport bypass else, game-ready GameModes or-chain,
    outer `while true do` around the game-ready wait).
0d. Lost gotos: `-- goto [N]` with an exact instruction-count join point become a flag set at
    the goto and `if not flag then` around the skipped code (`if flag then break end` where
    a loop is left).
0c. Double while loops: `while true do / while true do ... end / end` is one loop; drop the
    wrapper and move the post-loop code printed at the end of the body after the loop.
0.  TESTSET restoration: `X = Y` / `if [not] X then` where Y is the function's own register
    or param and the body is empty or writes X first is printed with the correct sense; mark
    it so Pass 1 leaves it, and move its right operand / lost ternary else into place.
1.  Truthiness inversions: flips bare `if [not] VAR then` / `elseif [not] VAR then` (TEST).
    Does NOT touch == ~= < > <= >= comparisons (those aren't inverted by the decompiler).
1b. Boolean materializations: `x = (a == b)` whose lost `else x = true` nested the rest of
    the block inside the if.
2.  Setlist bugs: replaces `-- setlist _rN (batch=K)` comments with table packing.
2b. Multi-return forwarding: `f(g())` printed with a fixed register range of stale values.
3.  Targeted patches: namecall varargs, game-ready break, stand farm bridge, first-spawn loop.
3b. Or-chains: `if a or b then REST end` printed as nested tests with an empty innermost if.
4.  Broken if/else: moves dropped right operands (`x = x or v`) and guarded call bodies
    (`if not c then pcall(fn) end`, also with comparisons) back inside their empty ifs.
4b. Constant ternaries: `x = c and K or D` (literal K) lost the path where c is false.
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
    own call-frame temporaries, copies of a register a call has clobbered, for-loop control
    registers read in the body, copies indexed with a service-only member, and decompiler
    locals that are dead or hold a scalar where a nested closure needs the outer value) are
    renamed.
13b. Closed upvalues: main-chunk registers reused after the closures that captured them get a
    new name for the captured range (evidence: kind change, a later call based at or below
    the register, argument setup for such a call, object use followed by a scalar).
13c. AutoPlay stage-click log: the one copy of the main logger that 13 cannot prove.
14. Garbled empty strings: 2-byte garbage constants that stand for "".
15. Item-name normalizer: the decompiled or-of-and chain is replaced by a first-match loop
    over the rules read back from the raw output.
"""

import bisect
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
BODY_END_RE = re.compile(r'^(?:do return\b.*\bend|return\b.*|break|-- goto \[\d+\]|_g\d+ = true)$')


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


CMP_GUARD_RE = re.compile(r'^(\s*)if (.+ (?:==|~=|<=|>=|<|>) .+) then$')


def fix_broken_if_else(lines):
    """Move dropped right operands (or guarded call bodies) back inside their empty if blocks."""
    lines = list(lines)
    count = 0
    for i in range(len(lines) - 3, -1, -1):
        m_if = BARE_IF_RE.match(lines[i].rstrip('\r\n'))
        if not m_if:
            # `if x == K then end` + body: comparisons keep their polarity, so the body
            # (a guarded call) goes inside as printed.
            m_cmp = CMP_GUARD_RE.match(lines[i].rstrip('\r\n'))
            if not m_cmp or lines[i + 1].rstrip('\r\n') != m_cmp.group(1) + 'end':
                continue
            nxt = ASSIGN_REG_RE.match(lines[i + 2].strip())
            var = f'_r{nxt.group(1)}' if nxt else None
            if var not in re.findall(r'_r\d+', m_cmp.group(2)):
                continue
            stop = _guarded_call_end(lines, i + 2, len(m_cmp.group(1)), var)
            if stop is None or stop <= i + 2:
                continue
            lines[i + 1:stop] = ['    ' + ln for ln in lines[i + 2:stop]] + [lines[i + 1]]
            count += 1
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


# Members that exist only on one service: a copy `X = R` indexed with one of them reads the
# variable named R that holds that service, whatever F's own R holds.
SERVICE_MEMBERS = {
    'RunService': ('RenderStepped', 'Heartbeat', 'Stepped', 'PreRender', 'PreSimulation',
                   'PostSimulation', 'BindToRenderStep', 'UnbindFromRenderStep'),
    'Players': ('LocalPlayer', 'GetPlayers', 'GetPlayerFromCharacter', 'GetPlayerByUserId',
                'PlayerAdded', 'PlayerRemoving'),
}
MEMBER_SERVICE = {m: s for s, ms in SERVICE_MEMBERS.items() for m in ms}
SVC_CALL_RE = re.compile(r'^\s*(_r\d+) = \1\((_r\d+), (_r\d+)\)\s*$')


def innermost_func(main, tok):
    G = main
    while True:
        for C in G.children:
            if C.header_tok <= tok <= C.end_tok:
                G = C
                break
        else:
            return G


def service_writes(text, funcs, toks):
    """{(func, R): {service}} for `R = game:GetService("S")` as decompiled:
    `R = R.GetService`, `[local] B = "S"`, `R = R(A, B)` within a few lines."""
    lines = text.split('\n')
    first_tok = {}
    for ti, t in enumerate(toks):
        first_tok.setdefault(t[2], ti)
    out = defaultdict(set)
    for k, s in enumerate(lines):
        m = SVC_CALL_RE.match(s)
        if not m or k + 1 not in first_tok:
            continue
        R, B = m.group(1), m.group(3)
        prev = lines[max(0, k - 4):k]
        if not any(p.strip() == f'{R} = {R}.GetService' for p in prev):
            continue
        names = [x.group(1) for x in (re.match(r'^\s*(?:local\s+)?' + B + r' = "(\w+)"\s*$', p)
                                      for p in prev) if x]
        if names:
            out[(innermost_func(funcs[0], first_tok[k + 1]), R)].add(names[-1])
    return out


BLOCK_OPEN = ('function', 'if', 'do', 'repeat')


def _matching_end(toks, open_tok):
    """Token index of the `end`/`until` closing the block opened at open_tok."""
    depth = 0
    for ti in range(open_tok, len(toks)):
        kind, val = toks[ti][0], toks[ti][1]
        if kind != 'kw':
            continue
        if val in BLOCK_OPEN:
            depth += 1
        elif val in ('end', 'until'):
            depth -= 1
            if depth == 0:
                return ti
    return len(toks) - 1


CALL_ARGS_RE = re.compile(r'^(?:_r\d+(?:\s*,\s*_r\d+)*\s*=\s*)?_r(\d+)\((.*)\)$')


def forced_upvalues(F, toks, lines_F, svc, tlines=None):
    """{R: {tok}} of reads in F that must be the enclosing variable named R although F's own
    R is in scope:
    - for-loop control registers (`in _rG, _rS, _rC` / `= _rA, _rB, _rC`) read in the loop
      body before any body write: source code cannot name them;
    - a copy `X = R` followed by `X = X.M` where M exists only on service S, F's own R is not
      that service, and the nearest enclosing function that owns R assigns it GetService(S);
    - a copy `_rK = _rJ` in a call's argument setup where _rJ is the callee or an earlier
      argument of the same call written in that setup: a compiler reloads from the source, never
      from a call temporary (`TeleportToPlaceInstance(id, job, job)`, `FireServer("X", "X")`)."""
    out = defaultdict(set)
    keys = sorted(lines_F)
    for idx, line in enumerate(keys):
        lt = lines_F[line]
        vals = [toks[t][1] for t in lt]
        if vals[0] == 'for' and vals[-1] == 'do':
            q = vals.index('in') if 'in' in vals else vals.index('=')
            open_ = {v for v in vals[q + 1:-1] if v != ',' and REG_NUM_RE.match(v or '')}
            last_line = toks[_matching_end(toks, lt[-1])][2]
            for line2 in keys[idx + 1:]:
                if not open_ or line2 >= last_line:
                    break
                lt2 = lines_F[line2]
                written = set()
                for pos, ti in enumerate(lt2):
                    v = toks[ti][1]
                    if v not in open_ or toks[ti][0] != 'name' or toks[ti - 1][1] in ('.', ':'):
                        continue
                    if classify_use(toks, lt2, pos) in ('owrite', 'cwrite'):
                        written.add(v)
                    else:
                        out[v].add(ti)
                open_ -= written
            continue
        start = 1 if vals[0] == 'local' else 0
        if len(vals) != start + 3 or vals[start + 1] != '=' or idx + 1 == len(keys) \
                or keys[idx + 1] != line + 1:
            continue
        X, R = vals[start], vals[start + 2]
        if not (REG_NUM_RE.match(X or '') and REG_NUM_RE.match(R or '')):
            continue
        nv = [toks[t][1] for t in lines_F[line + 1]]
        if len(nv) != 5 or nv[:4] != [X, '=', X, '.'] or nv[4] not in MEMBER_SERVICE:
            continue
        S = MEMBER_SERVICE[nv[4]]
        if S in svc.get((F, R), ()):
            continue
        G = F.parent
        while G is not None:
            if S in svc.get((G, R), ()):
                out[R].add(lt[start + 2])
                break
            if any(d.name == R for d in G.decls):
                break
            G = G.parent
    if tlines:
        for line in keys:
            src = tlines[line - 1]
            m = re.match(r'^\s*(?:local\s+)?_r(\d+)\s*=\s*_r(\d+)\s*$', src)
            if not m or int(m.group(2)) >= int(m.group(1)):
                continue
            K, J = int(m.group(1)), int(m.group(2))
            ind = _indent(src)
            wj = re.compile(r'^(?:local\s+)?_r' + str(J) + r'\s*=')
            call = None
            for q in range(line, min(line + 15, len(tlines))):
                st = tlines[q].strip()
                if _indent(tlines[q]) != ind or wj.match(st):
                    break
                c = CALL_ARGS_RE.match(st)
                if c:
                    call = (int(c.group(1)), [int(x) for x in re.findall(r'_r(\d+)', c.group(2))])
                    break
            if not call or not (call[0] <= J < K and K in call[1] and (J in call[1] or J == call[0])):
                continue
            if not any(wj.match(tlines[q].strip()) and _indent(tlines[q]) == ind
                       for q in range(max(0, line - 13), line - 1)):
                continue
            out[f'_r{J}'].add(lines_F[line][-1])
        # (d) a copy that is then called (or indexed) while F's latest own write of the source
        # is a literal that cannot be called (indexed); (e) the same source copied into both
        # the callee and an argument of one call (`f(f)`): the callee copy is the outer value.
        for idx, line in enumerate(keys):
            src = tlines[line - 1]
            m = re.match(r'^\s*(?:local\s+)?(_r\d+)\s*=\s*(_r\d+)\s*$', src)
            if not m or m.group(1) == m.group(2):
                continue
            X, R = m.group(1), m.group(2)
            ind = _indent(src)
            wx = re.compile(r'^(?:local\s+)?' + X + r'\s*=')
            use = None
            for q in range(line, min(line + 15, len(tlines))):
                st = tlines[q].strip()
                if _indent(tlines[q]) != ind:
                    break
                c = CALL_ARGS_RE.match(st)
                if c and f'_r{c.group(1)}' == X:
                    use = ('call', c, q)
                    break
                if re.match(r'^' + X + r'\s*=\s*' + X + r'\s*[.\[]', st):
                    use = ('index', None, q)
                    break
                if wx.match(st):
                    break
            if use is None:
                continue
            kind = None
            wr = re.compile(r'^\s*(?:local\s+)?' + R + r'\s*=\s*(.+)$')
            for q in reversed(keys[:idx]):
                w = wr.match(tlines[q - 1])
                if w:
                    kind = _value_kind(w.group(1))
                    break
            bad = ('string', 'number', 'boolean', 'table', 'nil') if use[0] == 'call' else ('number', 'boolean', 'nil')
            same_src_arg = use[0] == 'call' and any(
                re.match(r'^\s*(?:local\s+)?_r(\d+)\s*=\s*' + R + r'\s*$', tlines[q])
                and int(re.match(r'^\s*(?:local\s+)?_r(\d+)', tlines[q]).group(1))
                in [int(x) for x in re.findall(r'_r(\d+)', use[1].group(2))]
                for q in range(line, use[2]))
            if kind in bad or same_src_arg:
                out[R].add(lines_F[line][-1])
    return out


def find_leaks(funcs, toks=None, svc=None, tlines=None):
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
        # Reads that must be the enclosing variable (for-loop control registers, service members).
        for name, ftoks in forced_upvalues(F, toks, lines_F, svc or {}, tlines).items():
            ent = out.setdefault(F, {}).setdefault(name, {'decls': set(), 'uses': []})
            ent['forced'] = ftoks
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


def _enclosing_loop(toks, ti):
    """(header token, end token) of the innermost while/for/repeat around token ti within its
    function, or None."""
    depth = 0
    j = ti - 1
    while j >= 0:
        kind, v = toks[j][0], toks[j][1]
        if kind == 'kw':
            if v in ('end', 'until'):
                depth += 1
            elif v == 'function':
                if depth == 0:
                    return None
                depth -= 1
            elif v in ('if', 'repeat', 'do'):
                if depth:
                    depth -= 1
                elif v == 'repeat':
                    return j, _matching_end(toks, j)
                elif v == 'do':
                    h = j - 1
                    while h >= 0 and toks[h][2] == toks[j][2] and toks[h][1] not in ('for', 'while'):
                        h -= 1
                    if h >= 0 and toks[h][1] in ('for', 'while'):
                        return h, _matching_end(toks, j)
        j -= 1
    return None


def _else_ranges_after(toks, ti, limit):
    """Token ranges of the else branches of the ifs whose then-branch holds token ti."""
    ranges = []
    depth = 0
    j = ti + 1
    while j < limit:
        kind, v = toks[j][0], toks[j][1]
        if kind == 'kw':
            if v in BLOCK_OPEN:
                depth += 1
            elif v in ('end', 'until'):
                if depth:
                    depth -= 1
            elif v == 'else' and depth == 0:
                d, k = 0, j + 1
                while k < limit:
                    if toks[k][0] == 'kw':
                        if toks[k][1] in BLOCK_OPEN:
                            d += 1
                        elif toks[k][1] in ('end', 'until'):
                            if d == 0:
                                break
                            d -= 1
                    k += 1
                ranges.append((j, k))
                j = k
        j += 1
    return ranges


def classify_pair(F, R, toks, lines_F, is_main, forced=()):
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
            if ti in forced:
                decision[ti] = 'up'
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
            # The next occurrence decides; else branches beside the write's own branch do not
            # run after it (`if c then R = p else R = q end; use(R)`).
            own = False
            skip = _else_ranges_after(toks, ti, F.end_tok if F.end_tok is not None else len(toks))
            for occ2, kill2, last2 in events[idx + 1:]:
                if any(a <= last2 <= b for a, b in skip):
                    continue
                if any(k2 in ('oread', 'cread') for _, k2 in occ2):
                    own = True
                    break
                if kill2 or occ2:
                    break
            if not own:
                # loop-carried: read earlier in the enclosing loop body (next iteration)
                loop = _enclosing_loop(toks, ti)
                own = loop is not None and any(
                    loop[0] <= last2 < ti and any(k2 in ('oread', 'cread') for _, k2 in occ2)
                    for occ2, _, last2 in events)
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
    leaks = find_leaks(funcs, toks, service_writes(text, funcs, toks), text.split('\n'))
    lines_of = {F: own_line_tokens(F, toks) for F in leaks}
    decisions = {}                      # (F, R) -> ({tok: 'own' | 'up'}, timeline)
    for F, names in leaks.items():
        for R in names:
            if R.startswith('_r'):
                decisions[(F, R)] = classify_pair(F, R, toks, lines_of[F], F.parent is None,
                                                  names[R].get('forced', ()))
    depth = {}
    for F in funcs:                     # parents precede children in funcs
        depth[F] = 0 if F.parent is None else depth[F.parent] + 1

    # A reference that is not F-own binds to the nearest enclosing function owning R whose
    # register is live where the closure on the path is created (else it is a pass-through
    # upvalue and the next ancestor is checked). Main-chunk registers are globals: no claim.
    claims = {}                         # (G, tok) -> claiming Func
    refs = defaultdict(list)            # R -> [(G, tok)] for every not-own reference
    frame_cache = {}
    lazy = {}                           # (A, R) -> classify_pair for unleaked lexical owners
    dead_bound = set()                  # (A, R) whose dead local a nested reference passes
    lazy_claims = []                    # (G, tok, A, R) live-bound to such a local
    tlines = text.split('\n')

    def indexed_copy(tok):
        """The reference is a plain copy `X = R` whose next line indexes X."""
        ln = toks[tok][2]
        m = re.match(r'^(?:local\s+)?(_r\d+)\s*=\s*' + re.escape(toks[tok][1]) + r'$', tlines[ln - 1].strip())
        return bool(m) and ln < len(tlines) and re.match(
            r'^' + m.group(1) + r'\s*=\s*' + m.group(1) + r'\s*[.:\[]', tlines[ln].strip()) is not None

    def literal_before(A, name, tok):
        """Literal kind of A's latest own write of name before token tok (None if not a literal)."""
        kind = None
        rx = re.compile(r'^\s*(?:local\s+)?' + name + r'\s*=\s*(.*)$')
        for ln in sorted(lines_of[A]):
            if ln >= toks[tok][2]:
                break
            m = rx.match(tlines[ln - 1])
            if m:
                kind = _value_kind(m.group(1))
        return kind

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
                    # The decompiler's local in A. Dead where C is created (a call clobbered it):
                    # the name is an outer variable and A's register must be renamed.
                    if (A, name) not in lazy:
                        lines_of.setdefault(A, own_line_tokens(A, toks))
                        lazy[(A, name)] = classify_pair(A, name, toks, lines_of[A], False)
                    scalar = indexed_copy(tok) and literal_before(A, name, C.header_tok) in ('boolean', 'number')
                    if not scalar and live_at(lazy[(A, name)][1], C.header_tok):
                        lazy_claims.append((G, tok, A, name))
                        break
                    dead_bound.add((A, name))
                    C, A = A, A.parent
                    continue
                if (A, name) in decisions and live_at(decisions[(A, name)][1], C.header_tok):
                    claims[(G, tok)] = A
                    break
                C, A = A, A.parent
    for A, name in dead_bound:
        leaks.setdefault(A, {}).setdefault(name, {'decls': set(), 'uses': []})
        decisions[(A, name)] = lazy[(A, name)]
    for G, tok, A, name in lazy_claims:
        if (A, name) in dead_bound:
            claims[(G, tok)] = A

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


# --- RECURSIVE LOCAL FUNCTIONS (Pass 13e) ---
# `local function f(...)` (f in scope inside its own body) decompiles as
# `local f = function(...)`, where f inside the body is the enclosing scope's f - nil for the
# JSON settings encoder and the quest retry (`return f()`). A body that calls its own name
# with its own arity, where the name resolves to no enclosing local and the body never
# assigns it, is restored to `local function`. A name that does resolve to an enclosing
# local (the decompiler names upvalues by register, so the quest routine's retry
# `task.wait(0.5); return f()` read as the main chunk's ErrorMessageChanged hook) is taken
# only when every self-call is a tail call, the retry idiom.

LOCAL_FUNC_RE = re.compile(r'^(\s*)local (_r\d+) = function\(([^)]*)\)$')


def _subtree(F):
    out, stack = [], [F]
    while stack:
        G = stack.pop()
        out.append(G)
        stack.extend(G.children)
    return out


def _calls_self(F, reg, lines, arity):
    """True when F's body calls reg (directly or through a copy) with arity arguments and
    every use of reg in it is a read that resolves to no enclosing local, or every one is
    such a call in tail position."""
    uses = [u for G in _subtree(F) for u in G.uses if u[0] == reg]
    if not uses or any(u[3] not in ('global', 'up') for u in uses):
        return False
    if any(u[3] == 'up' for u in uses):
        return all(_tail_self_call(lines, u[1] - 1, reg, arity) for u in uses)
    called = False
    for _, line, _, _, _ in uses:
        stmt = lines[line - 1].strip()
        m = LHS_RE.match(stmt)
        if m and re.search(r'\b' + reg + r'\b', m.group(1)):
            return False                                   # assigned: a temp, not the function
        targets = [reg]
        c = re.fullmatch(r'(?:local\s+)?(_r\d+) = ' + reg, stmt)
        if c:
            targets.append(c.group(1))
        for q in range(line - 1, min(len(lines), line + 20)):
            for t in targets:
                k = re.search(r'\b' + t + r'\(([^()]*)\)', lines[q])
                if k and len([a for a in k.group(1).split(',') if a.strip()]) == arity:
                    called = True
    return called


def _tail_self_call(lines, k, reg, arity):
    """lines[k] is `return reg(args)`, or `_rX = reg` followed by `do return _rX(args) end`."""
    stmt = lines[k].strip()
    m = re.fullmatch(r'(?:do )?return ' + reg + r'\(([^()]*)\)(?: end)?', stmt)
    if m is None:
        c = re.fullmatch(r'(?:local\s+)?(_r\d+) = ' + reg, stmt)
        if c is None or k + 1 >= len(lines):
            return False
        m = re.fullmatch(r'(?:do )?return ' + c.group(1) + r'\(([^()]*)\)(?: end)?', lines[k + 1].strip())
    return m is not None and len([a for a in m.group(1).split(',') if a.strip()]) == arity


def fix_recursive_locals(text):
    lines = text.split('\n')
    funcs = analyze(tokenize(text))
    by_line = {F.line: F for F in funcs if F.parent is not None}
    count = 0
    for i, ln in enumerate(lines):
        m = LOCAL_FUNC_RE.match(ln)
        if not m or '...' in m.group(3):
            continue
        F = by_line.get(i + 1)
        arity = len([a for a in m.group(3).split(',') if a.strip()])
        if F is not None and _calls_self(F, m.group(2), lines, arity):
            lines[i] = f'{m.group(1)}local function {m.group(2)}({m.group(3)})'
            count += 1
    return '\n'.join(lines), count


# --- PARAMETER COLLISIONS (Pass 13d) ---
# Parameters are named _p1.._pN in every function, and an upvalue is named after the parent's
# variable, so a callback `function(_p1)` inside `function(_p1)` reads both as `_p1`
# (`AnimationPlayed:Connect(function(track) ... char.PrimaryPart`). A parameter of the function
# itself is indexed directly (`_p1.X`, SELF on _p1); an upvalue must first be copied into a
# register (`_rX = _p1` / `_rX = _rX.X`), which never happens for an own parameter in this
# output. The same holds for a parameter copied twice into one call (`handle(desc, char)`):
# the later copy is the outer value. Such reads keep the name; the own parameter is renamed.

def _header_params(lines, toks, G):
    m = re.search(r'function\s*\(([^)]*)\)', lines[toks[G.header_tok][2] - 1])
    return re.findall(r'_p\d+', m.group(1)) if m else []


def _uses_as_model(lines, a, b, P):
    """lines[a:b] use P as a model: P.PrimaryPart, or a copy X = P followed by X.PrimaryPart /
    X:FindFirstChild("HumanoidRootPart"|"Humanoid"|"Head")."""
    direct = re.compile(r'(?<![\w.])' + P + r'\.PrimaryPart\b')
    copy = re.compile(r'^\s*(?:local\s+)?(_r\d+\w*)\s*=\s*' + P + r'\s*$')
    for k in range(a, b):
        if direct.search(lines[k]):
            return True
        m = copy.match(lines[k])
        if not m:
            continue
        X = re.escape(m.group(1))
        near = [t.strip() for t in lines[k + 1:k + 4]]
        if any(re.match(r'^' + X + r'\s*=\s*' + X + r'\.PrimaryPart$', t) for t in near):
            return True
        if any(re.match(r'^' + X + r'\s*=\s*' + X + r'\.FindFirstChild$', t) for t in near) and \
                any(re.search(r'"(?:HumanoidRootPart|Humanoid|Head)"', t) for t in lines[k + 1:k + 6]):
            return True
    return False


def _model_arg_copy(lines, ln, X, P, ind, copy_re):
    """X = P (line ln) is one of several copies of P passed to the same call, and the callee
    (a named function defined in this file) uses the parameter in X's position as a model:
    X is the outer model, the other copy the callback's own value (`handleSound(char, sound)`)."""
    call = callee = None
    for q in range(ln, min(ln + 12, len(lines))):
        if _indent(lines[q]) != ind:
            return False
        c = CALL_ARGS_RE.match(lines[q].strip())
        if c:
            call, cq, base = re.findall(r'_r\d+\w*', c.group(2)), q, f'_r{c.group(1)}'
            break
    if not call or X not in call:
        return False
    setup = None
    for k in range(cq - 1, max(0, cq - 14), -1):
        m = re.match(r'^\s*(?:local\s+)?' + base + r'\s*=\s*(.+)$', lines[k])
        if m:
            if re.match(r'^[A-Za-z_]\w*$', m.group(1).strip()):
                callee, setup = m.group(1).strip(), k
            break
    if callee is None or setup >= ln - 1:
        return False
    others = [q for q in range(setup + 1, cq) if q != ln - 1 and _indent(lines[q]) == ind
              and copy_re.match(lines[q]) and copy_re.match(lines[q]).group(2) == P
              and copy_re.match(lines[q]).group(1) in call]
    if not others:
        return False
    head = next((k for k, t in enumerate(lines)
                 if re.match(r'^\s*(?:local\s+)?' + re.escape(callee) + r'\s*=\s*function\s*\(', t)), None)
    end = _close_of(lines, head) if head is not None else None
    if end is None:
        return False
    return _uses_as_model(lines, head + 1, end, f'_p{call.index(X) + 1}')


def fix_param_collisions(text):
    toks = tokenize(text)
    funcs = analyze(toks)
    lines = text.split('\n')
    depth = {}
    for F in funcs:
        depth[F] = 0 if F.parent is None else depth[F.parent] + 1
    edits = []
    count = 0
    copy_re = re.compile(r'^\s*(?:local\s+)?(_r\d+\w*)\s*=\s*(_p\d+)\s*$')
    for G in funcs[1:]:
        params = set(_header_params(lines, toks, G))
        anc = set()
        A = G.parent
        while A is not None and A.parent is not None:
            anc.update(_header_params(lines, toks, A))
            A = A.parent
        cand = params & anc
        if not cand:
            continue
        lg = own_line_tokens(G, toks)
        marked = defaultdict(set)
        for ln in sorted(lg):
            m = copy_re.match(lines[ln - 1])
            if not m or m.group(2) not in cand:
                continue
            X, P = m.groups()
            ind = _indent(lines[ln - 1])
            nx = lines[ln].strip() if ln < len(lines) else ''
            nx2 = lines[ln + 1].strip() if ln + 1 < len(lines) else ''
            through = re.match(r'^' + X + r'\s*=\s*' + X + r'\s*[.\[]', nx) or (
                re.match(r'^(?:local\s+)?_r\d+\w*\s*=\s*' + X + r'$', nx)
                and re.match(r'^' + X + r'\s*=\s*' + X + r'\.\w+$', nx2))
            dup = False
            if not through:
                dup = _model_arg_copy(lines, ln, X, P, ind, copy_re)
            if through or dup:
                marked[P].add(lg[ln][-1])
        for P, toks_up in marked.items():
            decl = next((d for d in G.blocks[0].locals.values() if d.name == P and d.is_param), None) \
                if G.blocks else None
            new = f'{P}o{depth[G]}'
            j = G.header_tok
            while toks[j][1] != ')':
                if toks[j][1] == P:
                    edits.append((toks[j][3], toks[j][3] + len(P), new))
                j += 1
            for H in _subtree(G):
                for name, _, tok, res, d in H.uses:
                    if name != P or tok in toks_up:
                        continue
                    if (decl is not None and d is decl) or (d is not None and d.is_param and d.block.func is G):
                        edits.append((toks[tok][3], toks[tok][3] + len(P), new))
            count += 1
    edits = sorted(set(edits))
    for a, b in zip(edits, edits[1:]):
        assert a[1] <= b[0], f'param rename overlap {a} {b}'
    out = []
    pos = 0
    for a, b, new in edits:
        out.append(text[pos:a])
        out.append(new)
        pos = b
    out.append(text[pos:])
    return ''.join(out), count


# --- MISPLACED ELSE (Pass 16) ---
# `if v then (A and B and X()) else (A and C and Y()) end` decompiles with the else attached
# to the innermost if: `if v then <setup A> if A then <setup B> if B then X else <setup A>
# if A then ... end end end end`, so the else branch runs only when B fails and never when v
# is false (the Server Hop Farm toggle could not switch the farm off). Signature: the else
# branch starts by re-deriving A. Fix: close the inner ifs and give the else to `if v`.

def fix_misplaced_else(text):
    lines = text.split('\n')
    count = 0
    k = 0
    while k < len(lines):
        if lines[k].strip() != 'else':
            k += 1
            continue
        e = _indent(lines[k])
        heads = []
        for ind in (e, e - 4, e - 8):
            j = (heads[-1] if heads else k) - 1
            while j >= 0 and not (lines[j].strip() and _indent(lines[j]) <= ind):
                j -= 1
            if j < 0 or _indent(lines[j]) != ind or not re.match(r'^if .+ then$', lines[j].strip()):
                break
            heads.append(j)
        if len(heads) != 3:
            k += 1
            continue
        b, a, p = heads
        setup = [ln.strip() for ln in lines[p + 1:a + 1] if ln.strip()]
        body = [ln.strip() for ln in lines[k + 1:k + 1 + len(setup)]]
        bend = block_end(lines, b, e)
        if not setup or body != setup or bend is None or bend + 2 >= len(lines) \
                or lines[bend + 1] != ' ' * (e - 4) + 'end' or lines[bend + 2] != ' ' * (e - 8) + 'end' \
                or any(ln.strip() == 'else' and _indent(ln) == e - 8 for ln in lines[p + 1:bend]):
            k += 1
            continue
        y = lines[k + 1:bend]
        _dedent(y, 0, len(y), 8)
        lines[p:bend + 3] = (lines[p:k] + [' ' * e + 'end', ' ' * (e - 4) + 'end', ' ' * (e - 8) + 'else']
                             + y + [' ' * (e - 8) + 'end'])
        count += 1
        k = p + 1
    return '\n'.join(lines), count


# --- AND-CHAIN WITH ELSE (Pass 17) ---
# `if A and B and C then <jump> end; REST` / `if A and B then else REST end` decompile to an
# and-chain of nested ifs ending in an empty if, with REST in the else of the OUTERMOST if,
# so REST runs only when A fails (findNearestItem skipped every item: its candidate code sat
# in the else of `if itemModule then`). REST must run whenever the whole chain fails.
# Shape: each level is plain setup lines followed by the next if as its last statement; the
# innermost if is empty; only the outermost has an else.

def _and_chain_inner(lines, i, o):
    """Index of the empty innermost if of the and-chain whose head is lines[i], or None."""
    e = block_end(lines, i, o)
    if e is None:
        return None
    els = next((q for q in range(i + 1, e) if lines[q].strip() == 'else' and _indent(lines[q]) == o), None)
    stop = els if els is not None else e
    k = i + 1
    while k < stop and _indent(lines[k]) == o + 4 and not COND_IF_RE.match(lines[k]):
        if not SIMPLE_STMT_RE.match(lines[k].strip()) or 'function' in lines[k]:
            return None
        k += 1
    if k >= stop or _indent(lines[k]) != o + 4 or not COND_IF_RE.match(lines[k]):
        return None
    ie = block_end(lines, k, o + 4)
    if ie is None or ie + 1 != stop:
        return None
    if ie == k + 1:
        return k
    if any(lines[q].strip() == 'else' and _indent(lines[q]) == o + 4 for q in range(k + 1, ie)):
        return None
    return _and_chain_inner(lines, k, o + 4)


def fix_and_chain_else(text):
    lines = text.split('\n')
    found = []
    for i, ln in enumerate(lines):
        if not COND_IF_RE.match(ln):
            continue
        o = _indent(ln)
        e = block_end(lines, i, o)
        if e is None:
            continue
        els = next((q for q in range(i + 1, e) if lines[q].strip() == 'else' and _indent(lines[q]) == o), None)
        if els is None:
            continue
        k = _and_chain_inner(lines, i, o)
        if k is not None:
            found.append((i, k, els, o))
    for n, (i, k, els, o) in enumerate(reversed(found)):
        flag = f'_and{100 + n}'
        lines[els:els + 1] = [' ' * o + 'end', ' ' * o + f'if not {flag} then']
        lines.insert(k + 1, ' ' * (_indent(lines[k]) + 4) + f'{flag} = true')
        lines.insert(i, ' ' * o + f'local {flag} = false')
    return '\n'.join(lines), len(found)


# --- FARM SELECTION MATCH (Pass 18) ---
# findNearestItem's "is this item selected" loop is
#   for _, s in ipairs(sel) do if (norm and norm(s) == nname) or s == name or (s == "Lucky
#   Arrow" and name ~= "Lucky Arrow" and item.Name and item.Name:find("Lucky Arrow")) then
#   matched = true end end
# The three-way or-of-ands decompiled with the body inside the third alternative and the
# second inverted (selecting Rokakaka matched everything else). Rebuilt from that reading.

SEL_LOOP_RE = re.compile(r'^(\s*)for _r40, _r41 in _r37, _r38, _r39 do\n\1    _r42 = (_r147v\d+)\n', re.M)


def fix_farm_selection(text):
    m = SEL_LOOP_RE.search(text)
    assert m, 'Pass 18: selection loop not found'
    assert len(SEL_LOOP_RE.findall(text)) == 1, 'Pass 18: selection loop not unique'
    lines = text.split('\n')
    head = text.count('\n', 0, m.start())
    pad, norm = m.group(1), m.group(2)
    end = block_end(lines, head, len(pad))
    body = '\n'.join(lines[head + 1:end])
    assert '"Lucky Arrow"' in body and '_r29.Name' in body and '_r35 = true' in body, 'Pass 18: body'
    lines[head + 1:end] = [pad + ln for ln in (
        '    if (' + norm + ' ~= nil and ' + norm + '(_r41) == _r36) or _r41 == _r32',
        '            or (_r41 == "Lucky Arrow" and _r32 ~= "Lucky Arrow" and _r29.Name ~= nil',
        '                and string.find(_r29.Name, "Lucky Arrow") ~= nil) then',
        '        _r35 = true',
        '        break',
        '    end')]
    return '\n'.join(lines), 1


AUTOPLAY_LOG_RE = re.compile(
    r'(\s*)_r21 = _r20o1\n'
    r'(\s*_r22 = string\n\s*_r22 = _r22\.format\n'
    r'\s*_r23o1 = "AutoPlay: Clicking stage %d button \[%s\] \(attempt %d\)")')


# --- ITEM-NAME NORMALIZER (Pass 15) ---
# normalizeItemName maps a raw item name to its canonical name through a chain of
# `if <find tests> then name = X; goto END end`. Its or-of-and conditions decompiled into
# nested tests whose jumps were lost (most names became "Yellow Candy") and the 46 gotos fell
# through (later, looser rules overrode earlier ones). The rules are read back from the raw
# output and the chain is replaced by a first-match loop over them.
# Reading a test `r = find(P)` + `if [not] r then`: a non-empty `if not r` continues the AND
# group (P); a non-empty `if r` ends an alternative (P alone); an empty if jumps on its printed
# condition - to the result if the next statement is another test (the group ends there), to
# failure if the next statement is the result (the literal is the opposite). `if s ~= "X" then`
# is an exact-name alternative.

def parse_item_name_rules(raw):
    """[(name, [[(kind, text, wanted)]])] from the raw normalizer chain."""
    L = [ln.rstrip('\r\n') for ln in raw]
    start = next(i for i, ln in enumerate(L) if ln.strip() == '_r14 = "(Redeemed Items)"')
    ret = next(i for i in range(start, len(L)) if L[i].strip() == '_r15 = _r145')

    def next_stmt(j):
        while j < ret and (not L[j].strip() or L[j].strip() == 'end' or L[j].strip().startswith('--')):
            j += 1
        return L[j].strip()
    rules, groups, cur, pat = [], [], [], None
    for i in range(start + 1, ret):
        st = L[i].strip()
        m = re.match(r'^(?:local )?_r17 = "(.*)"$', st)
        c = re.match(r'^if _r13 ~= "(.*)" then$', st)
        b = re.match(r'^_r14 = "(.*)"$', st)
        if m:
            pat = m.group(1)
        elif c:
            groups.append(cur + [('eq', c.group(1), True)])
            cur = []
        elif st == '_r15 = _r15(_r16, _r17, _r18, _r19)':
            t = L[i + 1].strip()
            assert t in ('if _r15 then', 'if not _r15 then') and pat is not None, f'item rules: line {i + 2}'
            neg = t == 'if not _r15 then'
            if L[i + 2].strip() != 'end':
                if neg:
                    cur.append(('find', pat, True))
                else:
                    groups.append(cur + [('find', pat, True)])
                    cur = []
            elif next_stmt(i + 3).startswith('_r14 = "'):
                cur.append(('find', pat, neg))
            else:
                groups.append(cur + [('find', pat, not neg)])
                cur = []
            pat = None
        elif b:
            if cur:
                groups.append(cur)
            rules.append((b.group(1), groups))
            groups, cur = [], []
    return rules


def fix_item_name_chain(text, rules):
    lines = text.split('\n')
    r = next(i for i, ln in enumerate(lines) if ln.strip() == '_r14 = "(Redeemed Items)"')
    ind = _indent(lines[r]) - 4
    assert lines[r - 1].strip() == 'if _r15 then' and lines[r + 1] == ' ' * ind + 'else', 'item rules: anchor'
    e = block_end(lines, r - 1, ind)
    assert e is not None and lines[e + 1].strip() == '_r15 = _r145', 'item rules: chain end'
    head = next(i for i in range(r, -1, -1) if lines[i].strip() == 'local _r146 = function(_p1)')

    def lit(k, v, w):
        return '{"%s", "%s", %s}' % (k, v, 'true' if w else 'false')
    table = ['_YBA_ITEM_NAME_RULES = {'] + [
        '    {"%s", {%s}},' % (name, ', '.join('{' + ', '.join(lit(*x) for x in g) + '}' for g in groups))
        for name, groups in rules] + ['}']
    pad = ' ' * (ind + 4)
    loop = [
        'for _, _rule in ipairs(_YBA_ITEM_NAME_RULES) do',
        '    local _hit = false',
        '    for _, _grp in ipairs(_rule[2]) do',
        '        local _ok = true',
        '        for _, _lit in ipairs(_grp) do',
        '            local _found',
        '            if _lit[1] == "eq" then',
        '                _found = _r13 == _lit[2]',
        '            else',
        '                _found = string.find(_r13, _lit[2], 1, true) ~= nil',
        '            end',
        '            if _found ~= _lit[3] then',
        '                _ok = false',
        '                break',
        '            end',
        '        end',
        '        if _ok then',
        '            _hit = true',
        '            break',
        '        end',
        '    end',
        '    if _hit then',
        '        _r14 = _rule[1]',
        '        break',
        '    end',
        'end',
    ]
    lines[r + 2:e] = [pad + ln for ln in loop]
    hpad = lines[head][:_indent(lines[head])]
    lines[head:head] = [hpad + ln for ln in table]
    return '\n'.join(lines), len(rules)


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


def _call_based(lines, ln, R):
    """True if the write `R = f` at line ln is the base of a call (`R = R(...)`, a for-in
    generator). A call result assigned to a live local is computed at the stack top and moved,
    so a call based at R means the local previously held in R is dead."""
    n = int(R[2:])
    ind = _indent(lines[ln - 1])
    call = re.compile(r'^(?:' + R + r'(?:\s*,\s*_r\d+)*\s*=\s*)?' + R + r'\(')
    for j in range(ln, min(ln + MAX_OPERAND, len(lines))):
        s = lines[j]
        if _indent(s) != ind:
            return False
        st = s.strip()
        if call.match(st):
            return True
        m = ASSIGN_REG_RE.match(st)
        if not m or int(m.group(1)) <= n:
            return False
    return False


def _call_arg(lines, ln, R):
    """True if the write at line ln sets up an argument of a call based below R: the call sits
    at the stack top, so R was free."""
    n = int(R[2:])
    ind = _indent(lines[ln - 1])
    j, stop = ln, min(ln + 80, len(lines))
    while j < stop:
        st = lines[j].strip()
        if _indent(lines[j]) != ind:
            return False
        c = CALL_STMT_RE.match(st)
        if c:
            return int(c.group(1)) < n and re.search(r'\b' + R + r'\b', st[st.index('('):]) is not None
        # argument setup: register loads, stores into an argument table, callback closures
        if not (ASSIGN_REG_RE.match(st) or re.match(r'^_r\d+(?:\.\w+|\[[^\]]+\])\s*=', st)):
            return False
        if FUNC_HEAD_RE.search(st):
            e = _close_of(lines, j)
            if e is None:
                return False
            j = e
        j += 1
    return False


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
    # A plain copy `R = S` carries the kind of S's latest write.
    for R, ws in writes.items():
        for n, (ti, ln, kind) in enumerate(ws):
            m = re.match(r'^\s*(?:local\s+)?_r\d+\s*=\s*(_r\d+)\s*$', lines[ln - 1])
            if kind is None and m and m.group(1) != R:
                src = max((w for w in writes.get(m.group(1), ()) if w[0] < ti), default=None)
                if src is not None and src[2] not in (None, 'nil'):
                    ws[n] = (ti, ln, src[2])
    # A call sits at the stack top, so a main-chunk call based at Q <= R means no local lives
    # in R from there on: a closure created before it keeps a closed upvalue.
    calls = [(lm[ln][0], int(c.group(1))) for ln in sorted(lm)
             for c in [CALL_STMT_RE.match(lines[ln - 1].strip())] if c]
    ctoks = [t for t, _ in calls]
    table = [[q for _, q in calls]]             # sparse table: range minimum of call bases
    while (1 << len(table)) <= len(calls):
        prev, half = table[-1], 1 << (len(table) - 1)
        table.append([min(prev[i], prev[i + half]) for i in range(len(prev) - half)])

    def min_call_base(a, b):
        lo, hi = bisect.bisect_right(ctoks, a), bisect.bisect_left(ctoks, b)
        if lo >= hi:
            return None
        k = (hi - lo).bit_length() - 1
        return min(table[k][lo], table[k][hi - (1 << k)])

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

    # Range minimum of line indents (blank lines and string continuations ignored), for
    # same_block in O(1).
    big = 1 << 30
    ind_tab = [[big] + [big if not ln.strip() or (q > 0 and lines[q - 1].endswith('\\')) else _indent(ln)
                        for q, ln in enumerate(lines)]]
    while (1 << len(ind_tab)) <= len(ind_tab[0]):
        prev_row, half = ind_tab[-1], 1 << (len(ind_tab) - 1)
        ind_tab.append([min(prev_row[i], prev_row[i + half]) for i in range(len(prev_row) - half)])

    def same_block(a, b):
        """Line a (1-based) is in the block of line b: same indent, nothing shallower between."""
        ind = _indent(lines[b - 1])
        if _indent(lines[a - 1]) != ind:
            return False
        if b <= a:
            return True
        k = (b - a).bit_length() - 1
        return min(ind_tab[k][a], ind_tab[k][b - (1 << k)]) >= ind

    obj_cache = {}

    def used_as_object(C, R):
        """C (or a nested closure) indexes R directly or through a plain copy."""
        key = (C, R)
        if key not in obj_cache:
            a, b = toks[C.header_tok][2], toks[C.end_tok][2]
            direct = re.compile(r'(?<![\w.])' + R + r'\s*[.:\[]')
            copy = re.compile(r'^\s*(?:local\s+)?(_r\d+\w*)\s*=\s*' + R + r'\s*$')
            hit = False
            for q in range(a - 1, b):
                if direct.search(lines[q]):
                    hit = True
                    break
                m = copy.match(lines[q])
                if m and q + 1 < b and re.match(r'^\s*' + re.escape(m.group(1)) + r'\s*=\s*'
                                                 + re.escape(m.group(1)) + r'\s*[.:\[]', lines[q + 1]):
                    hit = True
                    break
            obj_cache[key] = hit
        return obj_cache[key]

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
            # The latest write in the closure's own block dominates its creation; writes nested
            # in between (`x = f and f(k) or d` printed as ifs) belong to the same range.
            prev = max((w for w in ws if w[0] < C.header_tok and same_block(w[1], hline)), default=None)
            nxt = min((w for w in ws if w[0] > C.end_tok), default=None)
            if prev is None or nxt is None:
                continue
            # A nil the closure itself fills in is closure state; a nil it only reads is a
            # forward reference to a later main-chunk assignment and stays shared.
            own_state = prev[2] == 'nil' and R in written_by[C]
            if prev[2] == 'nil' and not own_state:
                continue
            local[R] = (prev, nxt)
            mb = min_call_base(C.end_tok, nxt[0])
            # A value the closure indexes as an object cannot later be a string/number/boolean.
            pk = prev[2] or ('object' if used_as_object(C, R) else None)
            # A new function value replacing a function is a new local in the reused register
            # (helpers redefined per section), not a reassignment.
            if own_state or (pk and nxt[2] and (pk != nxt[2] or pk == 'function')
                             and not (pk == 'object' and nxt[2] in ('table', 'function', 'nil'))) \
                    or (nxt[2] == 'function' and pk != 'function') \
                    or _call_based(lines, nxt[1], R) or _call_arg(lines, nxt[1], R) \
                    or (mb is not None and mb <= int(R[2:])):
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
        # A nil that is closure state has its own split.
        before = max((w for w in writes[R] if w[0] < prev[0]), default=None)
        fwd = before is not None and before[2] == 'nil' and (R, before[0]) not in splits
        start = before[0] if fwd else prev[0]
        if before is None:
            start = -1
        for C, names in caps.items():
            if start < C.header_tok < nxt[0]:
                for ti in names.get(R, ()):
                    edits.append((toks[ti][3], toks[ti][3] + len(R), new))
    edits = sorted(set(edits))
    for a, b in zip(edits, edits[1:]):
        assert a[1] <= b[0], f'conflicting closed-upvalue renames {a} {b}'
    out = []
    pos = 0
    for start, end, new in edits:
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
COPY_RE = re.compile(r'^(\s*)(?:local\s+)?(_r\d+|_p\d+) = (_r\d+|_p\d+)\s*$')
TEST_RE = re.compile(r'^(\s*)if (not )?(_r\d+|_p\d+) then\s*$')


MULTI_CALL_RE = re.compile(r'^\s*((?:_r\d+\s*,\s*)+_r\d+)\s*=\s*_r\d+\(')


def _is_own_source(F, Y, tok, res, toks, cache, lines):
    """True when Y is F's own register/param in VM terms (TESTSET), False for an upvalue."""
    if res == 'own':
        return True
    if Y.startswith('_p'):
        return False
    if not any(d.name == Y and d.tok < tok for d in F.decls):
        # Undeclared: own when F wrote it as one of several call results (CALL fills
        # consecutive registers of the calling function, never an upvalue).
        for name, line, t, _, _ in F.uses:
            if name == Y and t < tok:
                m = MULTI_CALL_RE.match(lines[line - 1])
                if m and Y in re.split(r'\s*,\s*', m.group(1)):
                    return True
        return False
    key = (id(F), Y)
    if key not in cache:
        cache[key] = classify_pair(F, Y, toks, own_line_tokens(F, toks), F.parent is None)[0]
    return cache[key].get(tok) == 'own'


LHS_RE = re.compile(r'^(?:local\s+)?((?:_r\d+|_p\d+)(?:\s*,\s*(?:_r\d+|_p\d+))*)\s*=\s*(.*)$')


def _operand_body(lines, i, ind, reg):
    """True if the if-body at lines[i] can be a TESTSET right operand: empty, or its first
    mention of reg is a write (`R = R` is GETUPVAL, also a write). A body that reads reg first
    (`x:Destroy()`) or never mentions it (`return true`) belongs to a plain MOVE + TEST."""
    e = block_end(lines, i, ind)
    if e is None or e == i + 1:
        return True
    tok = re.compile(r'\b' + reg + r'\b')
    for j in range(i + 1, e):
        st = lines[j].strip()
        if not tok.search(st):
            continue
        m = LHS_RE.match(st)
        return bool(m and tok.search(m.group(1))
                    and (m.group(2) == reg or not tok.search(m.group(2))))
    return False


def _chain_tail_test(lines, i, ind, reg):
    """True for an empty TESTSET that ends the innermost branch of an and-chain
    (`X = (A and B) or K`): its right operand `X = K` follows the chain's closing `end`, out
    of reach of the operand move. Left unmarked, Pass 3b rebuilds it as an or-flag chain."""
    return (ind >= 4 and i + 3 < len(lines)
            and lines[i + 1].rstrip('\r\n') == ' ' * ind + 'end'
            and lines[i + 2].rstrip('\r\n') == ' ' * (ind - 4) + 'end'
            and lines[i + 3].startswith(' ' * (ind - 4) + reg + ' = '))


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
        ind = len(f.group(1))
        if (_is_own_source(F, Y, ytoks[-1], res, toks, cache, lines)
                and _operand_body(lines, i, ind, f.group(3))
                and not _chain_tail_test(lines, i, ind, f.group(3))):
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

NEAREST_TAIL_RE = re.compile(
    r'(?P<i>[ ]*)if not _r23 then\n'
    r'(?P=i)    do return _r23 end\n'
    r'(?P=i)end\n'
    r'(?P=i)_r17 = nil\n'
    r'(?P<o>[ ]*)end\n'
    r'(?P=o)return _r17\n'
    r'(?P<p>[ ]*)end\n'
)


def _nearest_tail_repl(m):
    o, p = m.group('o'), m.group('p')
    assert len(m.group('i')) == len(o) + 4 and len(o) == len(p) + 4, 'repair (e): indents'
    return (f'{o}end\n'
            f'{o}if not _r23 then\n{o}    do return _r23 end\n{o}end\n'
            f'{o}_r17 = nil\n'
            f'{p}end\n'
            f'{p}return _r17\n')


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

    # (e) findNearestItem: `for pass = 1, 2 do ... for _, item in pairs(items) do <evaluate> end;
    # if best then return best end end; return nil`. The decompiler printed the code after the
    # item loop at the end of its body (returning the first accepted item, or nil when the last
    # one was skipped) and `return nil` inside the pass loop. Move both out.
    text = ''.join(lines)
    text, n = NEAREST_TAIL_RE.subn(_nearest_tail_repl, text)
    assert n == 1, f'repair (e): expected 1 match, got {n}'
    lines = split_lines(text)
    stats['nearest_tail'] = n

    return lines, stats


# --- UNRESOLVED GOTOS (Pass 0d) ---
# The decompiler prints jumps it could not structure as `-- goto [N]` and falls through, so
# skipped code runs (lost else branches, `continue`s, joins after if/elseif chains). N is the
# 1-based index of the target instruction in the function. Each decompiled line is one
# instruction except: `if` = 2 (test + jump), `else` = 1, the `end` of a loop = 1 (back-jump),
# other `end`s and `while true do` = 0, a closure = 1 (its body is a separate function).
# Dropped instructions (lost boolean loads) make the count drift. The target must be one of the
# structurally possible join points (later statements of the enclosing blocks, block ends) whose
# count equals N, common to all gotos with that N; within GOTO_TOLERANCE only a join that tests
# the flag set right before the goto (`x = true` ... `if x then break end`) is accepted. Gotos
# without such a target stay as comments (fall through, as before).
# Rewrite: `F = true` at the goto, the code between it and the target wrapped in
# `if not F then`, `if F then break end` where a loop is left.

GOTO_RE = re.compile(r'^(\s*)-- goto \[(\d+)\]\s*$')
FUNC_HEAD_RE = re.compile(r'function\s*\([^)]*\)\s*$')
BLOCK_HEAD_RE = re.compile(r'^(?:if .* then|while .* do|for .* do|do|repeat)$')
LOOP_HEAD_RE = re.compile(r'^(?:while .* do|for .* do|repeat)$')
FLAG_SET_RE = re.compile(r'^(_r\d+|_p\d+) = (?:true|false)$')
GOTO_TOLERANCE = 4


def _opens_block(st):
    st = st.replace(TS_MARK, '')
    return bool(BLOCK_HEAD_RE.match(st) or FUNC_HEAD_RE.search(st))


def _close_of(lines, k):
    """Index of the line closing the block opened at k (`end` / `until` at the same indent)."""
    ind = _indent(lines[k])
    for j in range(k + 1, len(lines)):
        s = lines[j]
        st = s.strip()
        if not st:
            continue
        d = _indent(s)
        if d == ind and (st == 'end' or st.startswith('until')):
            return j
        if d < ind:
            return None
    return None


def _stmt_last(lines, k):
    return _close_of(lines, k) if _opens_block(lines[k].strip()) else k


def _count_function(lines, h):
    """{line index: instruction index of the first instruction at or after it} for the own
    lines of the function whose header is lines[h], and the index of its closing `end`."""
    ind0 = _indent(lines[h])
    opener = {}
    seq = []                                   # (line, instruction index or None)
    idx = 0
    i = h + 1
    while i < len(lines):
        s = lines[i]
        st = s.strip()
        ind = _indent(s)
        if st == 'end' and ind == ind0:
            break
        n = 0
        if not st or (st.startswith('--') and not GOTO_RE.match(s)):
            n = 0
        elif FUNC_HEAD_RE.search(st):
            e = _close_of(lines, i)
            seq.append((i, idx + 1))
            idx += 1
            i = (e if e is not None else i) + 1
            continue
        elif re.match(r'^(if|elseif) .* then$', st):
            n = 2
            opener[ind] = 'if'
        elif st == 'while true do':
            opener[ind] = 'loop'
        elif re.match(r'^while .* do$', st):
            n = 2
            opener[ind] = 'loop'
        elif re.match(r'^for .* do$', st):
            n = 1
            opener[ind] = 'loop'
        elif st == 'end':
            n = 1 if opener.get(ind) == 'loop' else 0
        else:
            n = 1
        seq.append((i, idx + 1 if n else None))
        idx += n
        i += 1
    cnt = {}
    nxt = idx + 1
    for line, k in reversed(seq):
        if k is not None:
            nxt = k
        cnt[line] = nxt
    cnt[i] = idx + 1
    return cnt, i


def _body_of(lines, k):
    """(header, first body line, closing line, is_loop) of the innermost block whose body
    holds line k; None at function level."""
    ind = _indent(lines[k])
    for j in range(k - 1, -1, -1):
        s = lines[j]
        st = s.strip()
        if not st or _indent(s) >= ind:
            continue
        if st == 'else':
            hd = j - 1
            while hd >= 0 and not (_indent(lines[hd]) == _indent(s) and lines[hd].strip().startswith('if ')):
                hd -= 1
            return hd, j + 1, _close_of(lines, hd), False
        if not _opens_block(st) or FUNC_HEAD_RE.search(st):
            return None
        close = _close_of(lines, j)
        if st.startswith('if '):
            els = next((q for q in range(j + 1, close) if lines[q].strip() == 'else'
                        and _indent(lines[q]) == _indent(s)), None)
            if els is not None and els < k:
                return None                    # handled by the else case above
            if els is not None:
                close = els
        return j, j + 1, close, bool(LOOP_HEAD_RE.match(st.replace(TS_MARK, '')))
    return None


def _goto_candidates(lines, g, cnt, h):
    """Join points the goto at g can reach: {line: count} over the later statements and block
    ends of the enclosing blocks, plus the register assigned just before the goto (flag or
    value carried to the target). None if the goto is unreachable."""
    p = g - 1
    while p > h and not lines[p].strip():
        p -= 1
    carried = None
    if _indent(lines[p]) == _indent(lines[g]):
        if re.match(r'^(?:break|return\b|do return\b)', lines[p].strip()) or GOTO_RE.match(lines[p]):
            return None                        # unreachable: follows a break/return/goto
        m = ASSIGN_REG_RE.match(lines[p].strip())
        if m and not _opens_block(lines[p].strip()):
            carried = f'_r{m.group(1)}'
    cands = {}
    pos = g
    while True:
        body = _body_of(lines, pos)
        close = body[2] if body else _count_function(lines, h)[1]
        bind = _indent(lines[pos])
        j = _stmt_last(lines, pos)
        if close is None or j is None:
            break
        for q in range(j + 1, close):
            st = lines[q].strip()
            if st and _indent(lines[q]) == bind and st not in ('end', 'else') and not st.startswith('--'):
                cands[q] = cnt.get(q)
        cands[close] = cnt.get(close)
        if body is None:
            break
        pos = body[0]
    return carried, cands


def _splits_chain(lines, q, carried):
    """Target q reads the register written by the plain statement just before it (the middle
    of an expression), and not every goto carries that register."""
    p = q - 1
    while p >= 0 and not lines[p].strip():
        p -= 1
    if _indent(lines[p]) != _indent(lines[q]) or _opens_block(lines[p].strip()):
        return False
    m = ASSIGN_REG_RE.match(lines[p].strip())
    if not m:
        return False
    R = f'_r{m.group(1)}'
    st = lines[q].strip()
    a = LHS_RE.match(st)
    reads = re.search(r'\b' + R + r'\b', a.group(2) if a else st)
    return bool(reads) and any(c != R for c in carried)


def _skip_safe(lines, g, q, end):
    """No register written in the code a goto at g skips (up to target q) is read after q
    before being rewritten (linear scan to the function end): skipping it loses no value."""
    writes = set()
    for k in range(g + 1, q):
        m = re.match(r'^\s*(?:local\s+)?((?:_r\d+)(?:\s*,\s*_r\d+)*)\s*(?:=|$)', lines[k])
        if m:
            writes.update(re.findall(r'_r\d+', m.group(1)))
    for R in writes:
        rx = re.compile(r'\b' + R + r'\b')
        for k in range(q, end):
            st = lines[k].strip()
            if not rx.search(st):
                continue
            a = LHS_RE.match(st)
            if a and rx.search(a.group(1)) and not rx.search(a.group(2)):
                break                          # rewritten first: the skipped value is dead
            return False
    return True


def _after_exit(lines, q):
    """Position q directly follows a return/break in its block: reachable only by a jump, so a
    drifted count there is most likely a miss for the return itself."""
    p = q - 1
    while p >= 0 and not lines[p].strip():
        p -= 1
    st = lines[q].strip()
    ind = _indent(lines[q]) + (4 if st in ('end', 'else') or st.startswith('until') else 0)
    return _indent(lines[p]) == ind and re.match(r'^(?:return\b|do return\b|break$)', lines[p].strip()) is not None


def _overwrites_carried(target, carried):
    """The target statement assigns a register a goto carries (set right before jumping)."""
    a = LHS_RE.match(target.strip())
    return bool(a) and any(r and re.search(r'\b' + r + r'\b', a.group(1)) for r in carried)


def _choose_goto_target(lines, group, end=None):
    """One target for gotos that jump to the same instruction: the common candidate nearest N.
    group: [(g, N, carried, cands)]. Off-by-one/two counts (dropped instructions) are accepted
    only when skipping to the candidate loses no live value."""
    N = group[0][1]
    common = set(group[0][3])
    for _, _, _, cands in group[1:]:
        common &= set(cands)
    carried = [c for _, _, c, _ in group]
    best = None
    for q in common:
        c = group[0][3][q]
        if c is None or abs(c - N) > GOTO_TOLERANCE or _splits_chain(lines, q, carried):
            continue
        flag_test = any(r and re.match(r'^if (?:not )?' + r + r' then$', lines[q].strip())
                        and FLAG_SET_RE.match(lines[g - 1].strip() if lines[g - 1].strip() else '')
                        for g, _, r, _ in group)
        if c != N and not flag_test and (end is None or abs(c - N) > 2
                                         or not all(_skip_safe(lines, g, q, end) for g, _, _, _ in group)
                                         or _overwrites_carried(lines[q], carried)
                                         or _after_exit(lines, q)):
            continue                           # off-by-k picks must not skip or clobber a value
        key = (0 if flag_test else 1, abs(c - N), c > N, -q)
        if best is None or key < best[0]:
            best = (key, q)
    return best[1] if best else None


# Gotos read as `continue` of their innermost loop by inspection (the count drifts too far):
# findNearestItem's item loop skips blacklisted items, auto-sell-blacklisted items over their
# limit, and the tie-break after a better candidate.
GOTO_CONTINUE = [
    (('"Item_Spawns"', '._blacklistedItems', 'isAutoSellBlacklisted'), 279),
]
# Gotos read as the join after their enclosing if/else (skip the rest of the else body):
# findNearestItem's priority chain `if lucky arrow then 1 elseif lucky mask then 2 elseif
# candy then 3 else 4 end`.
GOTO_JOIN = [
    (('"Item_Spawns"', '._blacklistedItems', '"luckystonemask"'), 267),
]


def _join_target(lines, gotos):
    """The closing line of the innermost else body around every goto (shared), or None."""
    target = None
    for g in gotos:
        pos = g
        while True:
            body = _body_of(lines, pos)
            if body is None:
                return None
            if lines[body[1] - 1].strip() == 'else':
                break
            pos = body[0]
        if target is not None and body[2] != target:
            return None
        target = body[2]
    return target


def _continue_target(lines, gotos):
    """The closing line of the innermost loop body around every goto (shared), or None."""
    target = None
    for g in gotos:
        pos = g
        while True:
            body = _body_of(lines, pos)
            if body is None:
                return None
            if body[3]:
                break
            pos = body[0]
        if target is not None and body[2] != target:
            return None
        target = body[2]
    return target


def fix_gotos(lines):
    lines = [ln.rstrip('\r\n') for ln in lines]
    heads = [i for i, s in enumerate(lines) if FUNC_HEAD_RE.search(s.strip())]
    owner = {}
    for h in heads:                            # innermost function wins (later headers are nested)
        e = _close_of(lines, h)
        for q in range(h + 1, e if e is not None else h + 1):
            if GOTO_RE.match(lines[q]):
                owner[q] = h
    targets = {}                               # goto line -> target line
    for h in sorted(set(owner.values())):
        cnt, _ = _count_function(lines, h)
        groups = defaultdict(list)
        for g in sorted(q for q, o in owner.items() if o == h):
            c = _goto_candidates(lines, g, cnt, h)
            if c is not None:
                groups[int(GOTO_RE.match(lines[g]).group(2))].append((g,) + (0,) + c)
        fend = _close_of(lines, h)
        ftext = '\n'.join(lines[h:fend]) if fend is not None else ''
        for N, group in groups.items():
            group = [(g, N, carried, cands) for g, _, carried, cands in group]
            if any(n == N and all(a in ftext for a in anchors) for anchors, n in GOTO_CONTINUE):
                t = _continue_target(lines, [g for g, *_ in group])
            elif any(n == N and all(a in ftext for a in anchors) for anchors, n in GOTO_JOIN):
                t = _join_target(lines, [g for g, *_ in group])
            else:
                t = _choose_goto_target(lines, group, fend)
            if t is not None:
                for g, _, _, _ in group:
                    targets[g] = t
    # Markers keep positions stable while the rewrites insert lines.
    flag_of = {}
    for g, t in sorted(targets.items()):
        flag_of.setdefault(t, f'_g{len(flag_of) + 1}')
    marked = []
    tmark = {t: f'--@T{flag_of[t]}' for t in flag_of}
    for k, s in enumerate(lines):
        if k in tmark:
            pad = ' ' * (_indent(lines[k]) + (4 if lines[k].strip() in ('end', 'else')
                                                 or lines[k].strip().startswith('until') else 0))
            marked.append(pad + tmark[k])
        if k in targets:
            marked.append(' ' * _indent(s) + f'--@G{flag_of[targets[k]]} {GOTO_RE.match(s).group(2)}')
        else:
            marked.append(s)
    lines = marked
    declared = set()
    total = sum(1 for s in lines if GOTO_RE.match(s) or s.strip().startswith('--@G'))
    stats = {'resolved': len(targets), 'unresolved': total - len(targets)}
    for F in sorted(set(flag_of.values()), key=lambda f: int(f[2:])):
        while True:
            g = next((k for k, s in enumerate(lines) if s.strip().startswith(f'--@G{F} ')), None)
            if g is None:
                break
            n_orig = lines[g].split()[-1]
            t = next(k for k, s in enumerate(lines) if s.strip() == f'--@T{F}')
            tind = _indent(lines[t])
            ind = _indent(lines[g])
            pos = g
            edits = []                         # (insert index, line) applied bottom-up
            while True:
                body = _body_of(lines, pos)
                bind = _indent(lines[pos])
                j = _stmt_last(lines, pos)
                if j is not None and bind == tind:
                    edits.append(('wrap', j + 1, t, bind))
                    break
                if body is None or body[2] is None or j is None:
                    edits = None
                    break
                if body[3]:
                    edits.append(('ins', j + 1, ' ' * bind + f'if {F} then break end' + TS_MARK))
                else:
                    edits.append(('wrap', j + 1, body[2], bind))
                pos = body[0]
            if edits is None:
                lines[g] = ' ' * ind + f'-- goto [{n_orig}]'
                stats['resolved'] -= 1
                stats['unresolved'] += 1
                continue
            lines[g] = ' ' * ind + f'{F} = true'
            if F not in declared:
                declared.add(F)
                edits.append(('ins', pos, ' ' * tind + f'local {F} = false'))
            for e in sorted(edits, key=lambda e: e[1], reverse=True):
                if e[0] == 'ins':
                    lines.insert(e[1], e[2])
                    continue
                _, a, b, d = e
                region = [q for q in range(a, b) if lines[q].strip() and not lines[q].strip().startswith('--@')]
                if not region:
                    continue
                for q in range(a, b):
                    if lines[q].strip() and not lines[q - 1].endswith('\\'):
                        lines[q] = '    ' + lines[q]
                lines.insert(b, ' ' * d + 'end')
                lines.insert(a, ' ' * d + f'if not {F} then' + TS_MARK)
    lines = [s for s in lines if not s.strip().startswith('--@T')]
    return [s + '\n' for s in lines], stats


# --- DOUBLE WHILE LOOPS (Pass 0c) ---
# `while true do / while true do BODY end / end` with nothing else in the outer loop is one
# loop printed twice: the outer wrapper has no back-jump of its own, so `break` in BODY must
# exit (as printed it restarts BODY: the number formatter spins on any value below 1000), and
# the code after the loop is printed at the end of BODY (a trailing `return`, reached through
# the chain of last statements / else branches), which makes the loop run once.
# Fix: drop the wrapper and move that return after the loop.

def _last_stmt(lines, a, b, ind):
    for k in range(b - 1, a - 1, -1):
        st = lines[k].strip()
        if st and _indent(lines[k]) == ind and st not in ('end', 'else') and not st.startswith('--'):
            return k
    return None


def _trailing_return(lines, a, b, ind):
    """(index, block start, block indent) of the return ending the chain of last statements of
    lines[a:b] (indent ind), following if bodies and their else branches."""
    k = _last_stmt(lines, a, b, ind)
    if k is None:
        return None
    st = lines[k].strip()
    if st.startswith('return') or st.startswith('do return'):
        return k, a, ind
    if st.startswith('if ') and st.replace(TS_MARK, '').endswith(' then'):
        e = block_end(lines, k, ind)
        if e is None:
            return None
        els = [q for q in range(k + 1, e) if lines[q].strip() == 'else' and _indent(lines[q]) == ind]
        return _trailing_return(lines, (els[-1] if els else k) + 1, e, ind + 4)
    return None


LOOP_OPEN_RE = re.compile(r'^(?:while .* do|for .* do|repeat|.*\bfunction\s*\(.*\)\s*)$')


def _has_break(lines, a, b):
    """lines[a:b] contain a `break` outside nested loops and functions."""
    k = a
    while k < b:
        st = lines[k].strip()
        if st == 'break':
            return True
        if LOOP_OPEN_RE.match(st) and not st.endswith('end'):
            e = block_end(lines, k, _indent(lines[k]))
            k = (e if e is not None else b) + 1
            continue
        k += 1
    return False


def _post_loop_start(lines, a, r, ind):
    """First line of the post-loop code in the block lines[a:r] (indent ind) ending at return r:
    the statement after the last one that contains a break, or r itself."""
    starts = [k for k in range(a, r) if lines[k].strip() and _indent(lines[k]) == ind
              and lines[k].strip() not in ('end', 'else') and not lines[k].strip().startswith('--')]
    bounds = starts + [r]
    for q in range(len(starts) - 1, -1, -1):
        if _has_break(lines, starts[q], bounds[q + 1]):
            return bounds[q + 1]
    return r


def fix_double_whiles(lines):
    lines = list(lines)
    stats = {'collapsed': 0, 'returns_moved': 0}
    i = 0
    while i < len(lines) - 1:
        s0, s1 = lines[i].rstrip('\r\n'), lines[i + 1].rstrip('\r\n')
        ind = _indent(s0)
        if s0.strip() != 'while true do' or s1.strip() != 'while true do' or _indent(s1) != ind + 4:
            i += 1
            continue
        ei = block_end(lines, i + 1, ind + 4)
        eo = block_end(lines, i, ind)
        if ei is None or eo != ei + 1:
            i += 1
            continue
        tr = _trailing_return(lines, i + 2, ei, ind + 8)
        post = []
        if tr is not None:
            r, blk, bind = tr
            p = _post_loop_start(lines, blk, r, bind)
            post = lines[p:r + 1]
            _dedent(post, 0, len(post), bind - ind)
            del lines[p:r + 1]
            ei -= r + 1 - p
            eo -= r + 1 - p
        del lines[eo]
        del lines[i]
        ei -= 1
        _dedent(lines, i, ei + 1)
        if post:
            lines[ei + 1:ei + 1] = post
            stats['returns_moved'] += 1
        stats['collapsed'] += 1
    return lines, stats


# --- CONSTANT TERNARIES (Pass 4b) ---
# `x = cond and K1 or K2` with a literal K1 decompiles without the path where cond is false
# (that path jumps straight to the K2 operand), leaving x nil:
#   if cond then / x = K1 / if not x then / <K2 operand> / end / end
# A literal load followed by `if not x` is never hand-written, so the shape is unambiguous.
# Fix: add `else / <K2 operand>` to the outer if.

TERN_IF_RE = re.compile(r'^(\s*)if .+ then$')
TERN_LOAD_RE = re.compile(r'^(\s*)(?:local )?(_r\d+|_p\d+) = (?:-?\d+(?:\.\d+)?|0x[0-9a-fA-F]+|"(?:\\.|[^"\\])*"|true)$')


def fix_const_ternaries(lines):
    lines = list(lines)
    count = 0
    i = 0
    while i < len(lines) - 3:
        m = TERN_IF_RE.match(lines[i].rstrip('\r\n'))
        a = m and TERN_LOAD_RE.match(lines[i + 1].rstrip('\r\n'))
        ind = len(m.group(1)) if m else 0
        if not a or len(a.group(1)) != ind + 4 \
                or lines[i + 2].rstrip('\r\n') != ' ' * (ind + 4) + f'if not {a.group(2)} then':
            i += 1
            continue
        j = block_end(lines, i + 2, ind + 4)
        if j is None or j + 1 >= len(lines) or lines[j + 1].rstrip('\r\n') != ' ' * ind + 'end':
            i += 1
            continue
        ending = lines[i][len(lines[i].rstrip('\r\n')):]
        operand = lines[i + 3:j]
        _dedent(operand, 0, len(operand))
        lines[j + 1:j + 1] = [' ' * ind + 'else' + ending] + operand
        count += 1
        i = j + 2 + len(operand)
    return lines, count


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


# --- OR-OF-AND CHAINS (Pass 3a) ---
# `if A or (B and C) or D then REST end` over one register decompiles to nested tests in
# which the and-term ends in an empty test followed by more tests in the same block:
#   R = A / if not R then / R = B / if R then / R = C / if not R then / end /
#   R = D / if not R then / R = E / if R then / end / end / end / REST / end
# Reading of the printed (post-Pass-1) conditions: a non-empty `if not R` is an alternative
# of its own (R truthy reaches REST); a non-empty `if R` is a member of an and-term (R falsy
# goes to the next alternative); an empty test closes the current and-term - inside the
# chain REST is reached on the negation of its printed condition, as the last test on the
# printed condition itself. The busy checks `_r87 or (SF and SF.isEatingRoka) or
# getgenv()._YBA_IsEatingRoka or getgenv()._YBA_IsUsingItem` are the reference cases (as
# printed they returned "busy" whenever the first flag was clear).
# The chain becomes one `if <expr> then REST end`, the register loads folded into <expr>.
# Only chains with an empty test before the last one are taken; Pass 3b reads the others.

FOLD_RE = re.compile(r'^(?:local\s+)?(_r\d+) = (.+)$')
NAME_RE = re.compile(r'[A-Za-z_]\w*')
LITERAL_RE = re.compile(r'"[^"\\]*"|-?\d+(?:\.\d+)?|true|false|nil')


class _ChainAbort(Exception):
    pass


def _fold(expr, reg, rhs, args=None):
    """Value expression of reg after `reg = rhs`, given its previous value expr (None when
    unknown); None when rhs is not a plain load (name, field of a name, field of reg, call of
    reg whose arguments are literals loaded into the registers named in args)."""
    m = re.fullmatch(re.escape(reg) + r'\(((?:_r\d+)(?:\s*,\s*_r\d+)*)\)', rhs)
    if m and expr is not None and args:
        names = re.split(r'\s*,\s*', m.group(1))
        if all(a in args for a in names):
            return expr + '(' + ', '.join(args[a] for a in names) + ')'
        return None
    if NAME_RE.fullmatch(rhs) and rhs != reg and rhs not in KEYWORDS:
        return rhs
    m = re.fullmatch(r'([A-Za-z_]\w*)\.[A-Za-z_]\w*', rhs)
    if m and m.group(1) != reg and m.group(1) not in KEYWORDS:
        return rhs
    m = re.fullmatch(re.escape(reg) + r'(\.[A-Za-z_]\w*|\(\))', rhs)
    if m and expr is not None:
        return expr + m.group(1)
    return None


def _walk_chain(lines, start, end, ind, reg, expr, seq, st):
    """Collect the tests of reg in the block lines[start:end] (statements at indent ind) in
    printed order: seq gets (kind, expr, negated, line), kind 'empty', 'pos' or 'neg'.
    st['rest'] becomes (first line, end, indent) of REST: the first statement that is neither
    a foldable load of reg nor a test of it, together with the loads just before it.
    Raises _ChainAbort on any other shape."""
    k = start
    loads = None
    while k < end:
        s = lines[k].rstrip('\r\n')
        stmt = s.strip()
        if not stmt:
            k += 1
            continue
        if _indent(s) != ind:
            raise _ChainAbort
        m = FOLD_RE.match(stmt)
        if m and m.group(1) == reg:
            nxt = _fold(expr, reg, m.group(2).strip(), st['args'])
            if nxt is not None:
                expr = nxt
                loads = k if loads is None else loads
                k += 1
                continue
        elif (m and int(m.group(1)[2:]) > int(reg[2:])
                and LITERAL_RE.fullmatch(m.group(2).strip())):
            st['args'][m.group(1)] = m.group(2).strip()      # call argument for a later fold
            loads = k if loads is None else loads
            k += 1
            continue
        t = re.fullmatch(r'if (not )?' + re.escape(reg) + r' then', stmt)
        if t and expr is not None:
            e = block_end(lines, k, ind)
            if e is None or _has_else(lines, k + 1, e, ind):
                raise _ChainAbort
            neg = t.group(1) is not None
            loads = None
            if e == k + 1:
                seq.append(('empty', expr, neg, k))
            else:
                seq.append(('neg' if neg else 'pos', expr, neg, k))
                _walk_chain(lines, k + 1, e, ind + 4, reg, expr, seq, st)
                if st['rest'] is not None:
                    if any(lines[q].strip() for q in range(e + 1, end)):
                        raise _ChainAbort
                    return
                expr = None                # value after a nested block is unknown
            k = e + 1
            continue
        st['rest'] = (k if loads is None else loads, end, ind)
        return


def _or_and_expr(seq):
    """The chain's condition from its tests (see the Pass 3a notes)."""
    alts, group = [], []
    for n, (kind, expr, neg, _) in enumerate(seq):
        if kind == 'neg':
            if group:
                raise _ChainAbort
            alts.append(expr)
        elif kind == 'pos':
            group.append(expr)
        else:
            last = n == len(seq) - 1
            terms = group + [expr if neg != last else 'not ' + expr]
            alts.append(terms[0] if len(terms) == 1 else '(' + ' and '.join(terms) + ')')
            group = []
    if group or not seq or seq[-1][0] != 'empty':
        raise _ChainAbort
    return ' or '.join(alts)


def _reg_read_next(lines, k, reg, limit=300):
    """True when the first statement from lines[k] on that mentions reg reads it."""
    pat = re.compile(r'\b' + re.escape(reg) + r'\b')
    for j in range(k, min(len(lines), k + limit)):
        stmt = lines[j].strip()
        if not pat.search(stmt):
            continue
        m = LHS_RE.match(stmt)
        return not (m and pat.search(m.group(1)) and not pat.search(m.group(2)))
    return False


def _shift(line, delta):
    if not line.strip() or delta == 0:
        return line
    if delta > 0:
        return ' ' * delta + line
    assert not line[:-delta].strip(), 'Pass 3a: REST line less indented than its block'
    return line[-delta:]


def fix_or_and_chains(lines):
    lines = list(lines)
    count = 0
    i = 0
    while i < len(lines):
        t = re.match(r'^(\s*)if (?:not )?(_r\d+) then$', lines[i].rstrip('\r\n'))
        if not t:
            i += 1
            continue
        I, reg = len(t.group(1)), t.group(2)
        j = i
        while j > 0 and _indent(lines[j - 1]) == I and (FOLD_RE.match(lines[j - 1].strip()) or [0, 0])[1] == reg:
            j -= 1
        expr = None
        for q in range(j, i):
            expr = _fold(expr, reg, FOLD_RE.match(lines[q].strip()).group(2).strip())
            if expr is None:
                break
        e = block_end(lines, i, I)
        seq, st = [], {'rest': None, 'args': {}}
        try:
            if expr is None or e is None:
                raise _ChainAbort
            _walk_chain(lines, i, e + 1, I, reg, expr, seq, st)
            if st['rest'] is None or not any(kind == 'empty' for kind, *_ in seq[:-1]):
                raise _ChainAbort
            cond = _or_and_expr(seq)
        except _ChainAbort:
            i += 1
            continue
        rs, rend, rind = st['rest']
        # A chain whose register is read afterwards is a value expression (`return a or
        # (b and c) or true`): its trailing statements are another alternative, not a body.
        if _reg_read_next(lines, e + 1, reg) or _reg_read_next(lines, rs, reg) or any(
                _reg_read_next(lines, rs, a) or _reg_read_next(lines, e + 1, a) for a in st['args']):
            i += 1
            continue
        pad = ' ' * I
        # the loads above the root are folded into cond and dead; a declaration stays
        decl = [pad + f'local {reg}\n'] if any(ln.lstrip().startswith('local ') for ln in lines[j:i]) else []
        body = [_shift(ln, I + 4 - rind) for ln in lines[rs:rend]]
        lines[j:e + 1] = decl + [pad + f'if {cond} then\n'] + body + [pad + 'end\n']
        count += 1
        i += 1
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
# An else on the outermost if is the branch taken when the whole chain fails
# (`if a or b then REST else E end`) and becomes the flag test's else; the JSON encoder's
# `type(k) ~= "number" or k < 1 or k % 1 ~= 0` is the reference case.
# Applied only when every test in the chain shares a register, setups are plain
# assignments, no inner level has an else, and REST is non-empty.

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


def _else_line(lines, lo, hi, ind):
    """Index of the single plain `else` at indent ind in lines[lo:hi], or None (no else,
    several, or an elseif)."""
    found = None
    for k in range(lo, hi):
        s = lines[k].rstrip('\r\n')
        if _indent(s) != ind or not s.lstrip().startswith('else'):
            continue
        if s.strip() != 'else' or found is not None:
            return None
        found = k
    return found


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
    if relaxed and not shared and k == start + 1:
        shared = nregs or {'_'}          # a level holding only the next test: a pure or-chain
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
    # literal loads of the returned registers followed by the return (`return nil, nil, nil`)
    lit = r'^(?:local\s+)?_r\d+\s*=\s*(?:true|false|nil|-?\d[\d.]*|"[^"]*")$'
    return len(rest) >= 2 and re.match(r'^(?:do )?return\b', rest[-1].strip()) is not None \
        and all(re.match(lit, ln.strip()) for ln in rest[:-1])


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
        el = _else_line(lines, i + 1, e, I) if e is not None else None
        if e is None or (el is None and _has_else(lines, i + 1, e, I)):
            i += 1
            continue
        then_end = e if el is None else el
        rest = None
        for relaxed in (False, True):
            found = _find_chain(lines, i, I, then_end, regs, relaxed)
            if found is None:
                continue
            E, top_end, depth = found
            if any(lines[q].strip() for q in range(top_end + 1, then_end)):
                inside = [ln for ln in lines[top_end + 1:then_end] if ln.strip()]
                if relaxed and depth < 2 and not _relaxed_rest_ok(inside, lines[E]):
                    continue
                rest = lines[top_end + 1:then_end]      # REST inside the outer block
                tail_end = e + 1
                break
            if el is not None:
                continue
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
        if el is not None:
            # the outer else is the branch taken when the whole chain fails
            new += [pad + 'end\n', pad + f'if {flag} then\n'] + rest + lines[el:e + 1]
        else:
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


# --- REPEAT-UNTIL OR EXITS (Pass 8d) ---
# `repeat BODY until A or B` decompiles to a `while true do` without any exit whose body ends
# in the or-chain test (Pass 3b's `if _orN then`) holding the code that follows the loop:
#   while true do / BODY / local _orN = true / ... / if _orN then / AFTER / end / end
# (the stand-summon wait looped forever once the stand appeared). Taken only when the loop
# has no break of its own and is followed by more statements in its block (an exit was
# lost): the test becomes `if _orN then break end` and AFTER moves below the loop.
# When the decompiler also left the rest of the function inside the loop (after the test),
# the flag test is recognised by its timeout: its chain compares a literal with a register
# the loop body counts up (`t = t + 0.1` ... `5 < t`); the test and everything after it in
# the body are the code after the loop.

LOOP_HDR_RE = re.compile(r'^(?:while .* do|for .* do|repeat)$')


def _exits_loop(lines, lo, hi):
    """True when lines[lo:hi], the body of the loop opened at lines[lo - 1], contains a
    break belonging to that loop (not to a nested loop or function)."""
    for k in range(lo, hi):
        if lines[k].strip() != 'break':
            continue
        cur = _indent(lines[k])
        for j in range(k - 1, lo - 2, -1):
            st = lines[j].strip()
            if st and _indent(lines[j]) < cur:
                cur = _indent(lines[j])
                if LOOP_HDR_RE.match(st) or re.search(r'\bfunction\b', st):
                    if j == lo - 1:
                        return True
                    break
    return False


def _timeout_flag_test(lines, lo, hi, ind):
    """Index of the first `if _orN then` at indent ind in lines[lo:hi] whose chain (from
    `local _orN = true`) compares a number literal with a register counted up at indent ind
    before it or with an elapsed time computed in the chain (`R = R - start`), or None."""
    counted = set()
    flag_at = {}
    for k in range(lo, hi):
        if _indent(lines[k]) != ind:
            continue
        st = lines[k].strip()
        m = re.fullmatch(r'(_r\d+) = \1 \+ [\d.]+', st)
        if m:
            counted.add(m.group(1))
        m = re.fullmatch(r'local (_or\d+) = true', st)
        if m:
            flag_at[m.group(1)] = k
        m = re.fullmatch(r'if (_or\d+) then', st)
        if m and m.group(1) in flag_at:
            chain = lines[flag_at[m.group(1)]:k]
            elapsed = {x.group(1) for ln in chain
                       for x in [re.fullmatch(r'(_r\d+) = \1 - _r\d+', ln.strip())] if x}
            text = ' '.join(chain)
            if any(re.search(r'\b[\d.]+ <=? ' + r + r'\b|\b' + r + r' >=? [\d.]+', text)
                   for r in counted | elapsed):
                return k
    return None


def fix_repeat_or_exits(lines):
    lines = list(lines)
    count = 0
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() != 'while true do':
            continue
        w = _indent(lines[i])
        e = block_end(lines, i, w)
        if e is None or _exits_loop(lines, i + 1, e):
            continue
        j = _timeout_flag_test(lines, i + 1, e, w + 4)
        k = block_end(lines, j, w + 4) if j is not None else None
        if k is not None and k < e and not _has_else(lines, j + 1, k, w + 4):
            inner, rest = list(lines[j + 1:k]), list(lines[k + 1:e])
            _dedent(inner, 0, len(inner), 8)
            head = [' ' * (w + 8) + 'break\n', lines[k], lines[e]] + inner
            # A loop ending an else-less if block (`if not found then summon; repeat ...
            # until ... end`): the rest of the function follows that if, not the loop.
            h = i - 1
            while h >= 0 and not (lines[h].strip() and _indent(lines[h]) < w):
                h -= 1
            eh = block_end(lines, h, w - 4) if h >= 0 and _indent(lines[h]) == w - 4 \
                and re.fullmatch(r'if .+ then', lines[h].strip()) else None
            if eh == e + 1 and not _has_else(lines, h + 1, eh, w - 4):
                _dedent(rest, 0, len(rest), 8)
                lines[j + 1:eh + 1] = head + [lines[eh]] + rest
            else:
                _dedent(rest, 0, len(rest), 4)
                lines[j + 1:e + 1] = head + rest
            count += 1
            continue
        after = next((q for q in range(e + 1, len(lines)) if lines[q].strip()), None)
        if after is None or _indent(lines[after]) != w or lines[after].strip() in ('end', 'return'):
            continue
        k = e - 1
        if lines[k].rstrip('\r\n') != ' ' * (w + 4) + 'end':
            continue
        j = k - 1
        while j > i and not (lines[j].strip() and _indent(lines[j]) == w + 4):
            j -= 1
        if not re.fullmatch(r'if _or\d+ then', lines[j].strip()) or block_end(lines, j, w + 4) != k:
            continue
        moved = list(lines[j + 1:k])
        if not moved:
            continue
        _dedent(moved, 0, len(moved), 8)
        lines[j + 1:k] = [' ' * (w + 8) + 'break\n']
        e = block_end(lines, i, w)
        lines[e + 1:e + 1] = moved
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
    item_rules = parse_item_name_rules(lines)

    # Pass 0b: Structural repairs on the raw output (blocks left open to end of file)
    lines, sr_stats = fix_structural_repairs(lines)
    sr_total = sum(sr_stats.values())
    print(f"Pass 0b: {sr_total} structural repairs: " + ', '.join(f'{k}={v}' for k, v in sr_stats.items()))

    # Pass 0d: Unresolved gotos (before 0c: instruction counts need the printed structure)
    lines, gt_stats = fix_gotos(lines)
    print(f"Pass 0d: Resolved {gt_stats['resolved']} gotos, {gt_stats['unresolved']} left as comments")

    # Pass 0c: Double while loops (one loop printed with a wrapper)
    lines, dw_stats = fix_double_whiles(lines)
    print(f"Pass 0c: Collapsed {dw_stats['collapsed']} double while loops, "
          f"moved {dw_stats['returns_moved']} post-loop returns")

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

    # Pass 3a: Or-of-and chains with a mid-chain empty test (before Pass 3b reads them)
    fixed_lines, oa_count = fix_or_and_chains(fixed_lines)
    print(f"Pass 3a: Rebuilt {oa_count} or-of-and chain conditions")

    # Pass 3b: Or-chains (must run before Pass 4 and Pass 11 read the empty ifs)
    fixed_lines, oc_count = fix_or_chains(fixed_lines)
    print(f"Pass 3b: Fixed {oc_count} or-chain conditions")

    # Pass 4: Fix broken if/else reconstruction
    fixed_lines, ie_count = fix_broken_if_else(fixed_lines)
    print(f"Pass 4: Fixed {ie_count} broken if/else blocks")

    # Pass 4b: Constant ternaries missing their else path (needs Pass 4's moved operands)
    fixed_lines, ct_count = fix_const_ternaries(fixed_lines)
    print(f"Pass 4b: Restored {ct_count} constant-ternary else branches")

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

    # Pass 8d: repeat-until-or loops whose exit test kept the code after the loop
    fixed_lines, ro_count = fix_repeat_or_exits(fixed_lines)
    print(f"Pass 8d: Restored {ro_count} repeat-until-or loop exits")

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

    # Pass 13d: Parameter collisions (callback params named like the enclosing function's)
    full_text, pc_count = fix_param_collisions(full_text)
    print(f"Pass 13d: Renamed {pc_count} parameters shadowing an enclosing parameter")
    full_text, rl_count = fix_recursive_locals(full_text)
    print(f"Pass 13e: Restored {rl_count} recursive local functions")

    # Pass 13c: AutoPlay stage-click log (collision 13 cannot prove: the function's own _r20 is
    # the second return of a call, the log call needs the main logger _r20)
    full_text, n = AUTOPLAY_LOG_RE.subn(r'\1_r21 = _r20\n\2', full_text)
    assert n == 1, f'Pass 13c: expected 1 AutoPlay log copy, got {n}'
    print("Pass 13c: Fixed AutoPlay stage-click log copy")

    # Pass 14: Garbled empty strings
    full_text, gs_count = fix_garbled_strings(full_text)
    print(f"Pass 14: Replaced {gs_count} garbled empty-string constants")

    # Pass 16: Else branches attached to the wrong if
    full_text, me_count = fix_misplaced_else(full_text)
    print(f"Pass 16: Moved {me_count} misplaced else branches to their if")

    # Pass 17: And-chains whose else belongs to the whole chain
    full_text, ac_count = fix_and_chain_else(full_text)
    print(f"Pass 17: Rebuilt {ac_count} and-chains with a misattached else")

    # Pass 18: findNearestItem selection match
    full_text, _ = fix_farm_selection(full_text)
    print("Pass 18: Rebuilt findNearestItem selection match")

    # Pass 15: Item-name normalizer chain rebuilt from its rules (after Pass 14: rule strings are clean)
    full_text, in_count = fix_item_name_chain(full_text, item_rules)
    print(f"Pass 15: Rebuilt item-name normalizer ({in_count} rules)")

    full_text = full_text.replace(TS_MARK, '')
    fixed_lines = split_lines(full_text)

    with open(output_file, 'w', encoding='latin-1') as f:
        f.writelines(fixed_lines)

    total = sr_total + ts_total + inv_count + bm_count + sl_stats['fixed'] + mr_count + oc_count + wc_count + lt_stats['yield_moved'] + lt_stats['success_break'] + tp_total + ie_count + va_count + tb_count + br_count + ks_count + gb_count + sy_count + pu_count + dr_count + gis_count + sc_stats['pairs'] + cu_count + gs_count
    print(f"\nTotal: {total} fixes applied")
    print(f"Output written to {output_file} ({len(fixed_lines)} lines)")


if __name__ == '__main__':
    main()
