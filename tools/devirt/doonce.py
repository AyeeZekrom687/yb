"""Make do-once blocks and continue jumps visible as loops (semantics-preserving bytecode rewrite).

Luau `repeat ... until true` (with `break` inside) and `continue` compile to forward jumps that
Lua 5.1 can only express with goto. For such a jump target T, wrap the region [S, T) in
`while true do <region> break end`: insert `JMP +1` (the break) and `JMP S` (the back edge) at T.
The jumps to T become breaks of the new loop. Control flow is unchanged: every path that reached
T still reaches it (the inserted break leads there), and the back edge is never taken.
"""
from luac51 import OP, decode
from unthread import _pseudo, _uncond, loops, TESTS
from chains import shift_marks

JMP_OPS = {OP['JMP'], OP['FORLOOP'], OP['FORPREP']}


def _jmp_word(sbx):
    return OP['JMP'] | ((sbx + 131071) << 14)


def _set_sbx(word, sbx):
    return (word & 0x3FFF) | ((sbx + 131071) << 14)


def _edges(ins, pseudo):
    """[(pc, target)] of every jump-like instruction (JMP, FORLOOP, FORPREP)."""
    out = []
    for pc, (op, a, b, c, bx, sbx) in enumerate(ins):
        if not pseudo[pc] and op in JMP_OPS:
            out.append((pc, pc + 1 + sbx))
    return out


def find_region(ins, pseudo, live, nact, target):
    """Largest S <= every jump source into target such that [S, target) is single-entry,
    exits only to target, and starts at a statement boundary. None when there is no such S."""
    edges = _edges(ins, pseudo)
    # the loop-entry jumps (FORPREP, and the prep JMP of a generic for) also reach the continue
    # point; they are not continue jumps
    prep = None
    if ins[target][0] == OP['TFORLOOP']:
        prep = target + 1 + ins[target + 1][5]   # the JMP at target+1 goes back to prep + 1
    sources = [pc for pc, t in edges if t == target and pc < target
               and ins[pc][0] == OP['JMP'] and pc != prep]
    if not sources:
        return None
    for s in range(min(sources), -1, -1):
        if pseudo[s]:
            continue
        if s > 0 and not pseudo[s - 1] and (ins[s - 1][0] in TESTS or
                                           ins[s - 1][0] == OP['LOADBOOL'] and ins[s - 1][3]):
            continue                     # inside a test/jump pair or a skipped LOADBOOL
        if live[s] >> nact[s]:
            continue                     # a temp is live at s: not a statement boundary
        ok = True
        for pc, t in edges:
            inside_src = s <= pc < target
            if not inside_src and s < t < target:
                ok = False               # entry into the middle of the region
                break
            if inside_src and not (s <= t <= target):
                ok = False               # exit other than to target
                break
        if ok:
            return s
    return None


def insert_loop(proto, s, t):
    """Insert `JMP +1; JMP s` before pc t and relocate jumps. Jumps to t go to the new exit."""
    code = proto.code
    ins = [decode(w) for w in code]
    pseudo = _pseudo(proto, ins)
    new = []
    for pc, w in enumerate(code):
        if pc == t:
            new.append(_jmp_word(1))                 # break: to the old t (now t + 2)
            new.append(_jmp_word(s - (t + 1) - 1))   # back edge at t + 1 to s
        op = ins[pc][0]
        if not pseudo[pc] and op in JMP_OPS:
            dst = pc + 1 + ins[pc][5]
            npc = pc + (2 if pc >= t else 0)
            ndst = dst + (2 if dst >= t else 0)
            w = _set_sbx(w, ndst - (npc + 1))
        new.append(w)
    proto.code = new
    shift_marks(proto, t, 2)
    if proto.lineinfo:
        proto.lineinfo = proto.lineinfo[:t] + [proto.lineinfo[t]] * 2 + proto.lineinfo[t:]


STATEMENT = {OP['SETTABLE'], OP['SETGLOBAL'], OP['SETUPVAL'], OP['SETLIST'], OP['RETURN'],
             OP['TAILCALL'], OP['FORPREP'], OP['FORLOOP'], OP['TFORLOOP'], OP['CLOSE']}


def jump_into_else(proto):
    """Targets of forward JMPs that unluac reads as an if-else skip although a test after
    statements of the supposed then-block jumps to the supposed else start. Such a JMP is a
    continue (or a skip over the rest of a block); unluac would silently build a wrong if-else,
    so its target must become the exit of a do-once loop."""
    from locinfer import Code
    code = Code(proto)
    code.liveness()
    ins, pseudo = code.ins, code.pseudo
    exits = {x for _, _, x in loops(ins, pseudo)}
    into = {}                       # pc -> [test-pair JMPs aiming at it]
    jumps = []                      # (source, target) of every JMP
    for q, (op, a, b, c, bx, sbx) in enumerate(ins):
        if op == OP['JMP'] and not pseudo[q]:
            jumps.append((q, q + 1 + sbx))
            if not _uncond(ins, pseudo, q):
                into.setdefault(q + 1 + sbx, []).append(q)
    out = set()
    for j, (op, a, b, c, bx, sbx) in enumerate(ins):
        e = j + 1 + sbx
        if not _uncond(ins, pseudo, j) or e <= j + 1 or e in exits:
            continue                # not a forward skip, or a break
        # an else block [j+1, e) is entered only at its start: a jump from before it to a join
        # in its interior (a pc also reached by fallthrough) means j skips the rest of an
        # enclosing block instead. Not joins: block starts after a JMP or RETURN (luac threads
        # nested else skips to the outermost end, so an outer else starts inside [j+1, e)) and
        # the LOADBOOL value ends of comparisons and and/or expressions
        if any(q < j and j + 1 < t < e and ins[t][0] != OP['LOADBOOL'] and _falls_into(ins, pseudo, t)
               for q, t in jumps):
            out.add(e)
            continue
        srcs = [q for q in into.get(j + 1, ()) if q < j]
        if len(srcs) < 2:
            continue
        lo, hi = min(srcs), max(srcs)
        for pc in range(lo + 1, hi):
            if pseudo[pc]:
                continue
            o, oc, osbx = ins[pc][0], ins[pc][3], ins[pc][5]
            if (o in STATEMENT or o == OP['CALL'] and oc == 1 or o == OP['JMP'] and osbx < 0
                    or not _consumed(code, pc, hi)):
                out.add(e)
                break
    return out


def _falls_into(ins, pseudo, t):
    """True when the instruction before t can fall through to t."""
    q = t - 1
    while q > 0 and pseudo[q]:
        q -= 1
    op = ins[q][0]
    if op == OP['RETURN'] or _uncond(ins, pseudo, q):
        return False
    return not (op == OP['LOADBOOL'] and ins[q][3])


def _consumed(code, pc, hi):
    """True when every register written at pc is read on the straight-line path up to and
    including the next condition test (stopping at hi): an instruction of an if condition only
    feeds the condition's tests. The path continues through and/or value tests and comparison
    values (LOADBOOL pairs) to their join. A register overwritten before it is read is not
    consumed."""
    ins = code.ins
    w = code.writes[pc]
    if not w or ins[pc][0] in TESTS or ins[pc][0] == OP['JMP']:
        return True
    q = pc
    while True:
        if len(code.succ[q]) == 1:
            q = code.succ[q][0]
        elif code.value_test(q) is not None:
            q = code.value_test(q)
        elif _compare_value(ins, q):
            q += 4
        else:
            break
        if q > hi:
            break
        w &= ~code.reads[q]
        if not w:
            return True
        if (ins[q][0] in TESTS and code.value_test(q) is None and not _compare_value(ins, q)
                or code.kills[q] & w):
            return False
    return not w


def _compare_value(ins, q):
    """`EQ/LT/LE; JMP +1; LOADBOOL r 0 1; LOADBOOL r 1 0`: a comparison computed as a value."""
    return (ins[q][0] in (OP['EQ'], OP['LT'], OP['LE']) and q + 3 < len(ins)
            and ins[q + 1][0] == OP['JMP'] and ins[q + 1][5] == 1
            and ins[q + 2][0] == OP['LOADBOOL'] and ins[q + 2][3] == 1
            and ins[q + 3][0] == OP['LOADBOOL'] and ins[q + 3][1] == ins[q + 2][1])
