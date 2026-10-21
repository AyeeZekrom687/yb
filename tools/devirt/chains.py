"""Rewrite mixed and/or value chains into the TESTSET form (semantics-preserving bytecode rewrite).

unluac mis-decompiles value expressions such as `a or (b and c)` when every operand is computed
into the destination register r and tested with TEST r. luac's own code for `y = a or (b and c)`
(destination already a local) computes each non-final operand into r+1 and uses
TESTSET r r+1 c, which unluac decompiles correctly. This module rewrites the first form into the
second: shift each non-final operand's registers >= r up by one and turn its TEST into TESTSET.
TESTSET r r+1 c copies r+1 into r exactly when it jumps, so r holds the same value on every path.
"""
from luac51 import OP, decode

TESTS = {OP['EQ'], OP['LT'], OP['LE'], OP['TEST'], OP['TESTSET']}
ARITH = set(range(OP['ADD'], OP['POW'] + 1))

# which fields hold registers (RK fields count only below 256)
REG_A = {OP['MOVE'], OP['LOADK'], OP['LOADBOOL'], OP['LOADNIL'], OP['GETUPVAL'], OP['GETGLOBAL'],
         OP['GETTABLE'], OP['SETGLOBAL'], OP['SETUPVAL'], OP['SETTABLE'], OP['NEWTABLE'], OP['SELF'],
         OP['UNM'], OP['NOT'], OP['LEN'], OP['CONCAT'], OP['TEST'], OP['TESTSET'], OP['CALL'],
         OP['TAILCALL'], OP['RETURN'], OP['FORLOOP'], OP['FORPREP'], OP['TFORLOOP'], OP['SETLIST'],
         OP['CLOSE'], OP['CLOSURE'], OP['VARARG']} | ARITH
REG_B = {OP['MOVE'], OP['LOADNIL'], OP['GETTABLE'], OP['SELF'], OP['UNM'], OP['NOT'], OP['LEN'],
         OP['CONCAT'], OP['TESTSET']}
REG_C = {OP['CONCAT']}
RK_B = {OP['SETTABLE'], OP['EQ'], OP['LT'], OP['LE']} | ARITH
RK_C = {OP['GETTABLE'], OP['SETTABLE'], OP['SELF'], OP['EQ'], OP['LT'], OP['LE']} | ARITH


def _abc(op, a, b, c):
    return op | (a << 6) | (c << 14) | (b << 23)


def _shift(word, r):
    """Raise every register operand >= r by one."""
    op, a, b, c, bx, sbx = decode(word)
    up = lambda x: x + 1 if x >= r else x
    if op in REG_A:
        a = up(a)
    if op in REG_B or op in RK_B and b < 256:
        b = up(b)
    if op in REG_C or op in RK_C and c < 256:
        c = up(c)
    if op in (OP['LOADK'], OP['GETGLOBAL'], OP['SETGLOBAL'], OP['CLOSURE']):
        return op | (a << 6) | (bx << 14)
    if op in (OP['JMP'], OP['FORLOOP'], OP['FORPREP']):
        return op | (a << 6) | ((sbx + 131071) << 14)
    return _abc(op, a, b, c)


def _shift_pseudo(word, r):
    """CLOSURE upvalue word: MOVE 0 B captures register B."""
    op, a, b, c, bx, sbx = decode(word)
    if op == OP['MOVE'] and b >= r:
        b += 1
    return _abc(op, a, b, c)


def chain_start(code, first, r):
    """First pc of the straight-line computation of the first operand that ends at `first`."""
    ins, pseudo = code.ins, code.pseudo
    targets = set()
    for pc in range(code.n):
        if not pseudo[pc] and ins[pc][0] in (OP['JMP'], OP['FORLOOP'], OP['FORPREP']):
            targets.add(pc + 1 + ins[pc][5])
    s = first
    while s > 0:
        q = s - 1
        while q > 0 and pseudo[q]:
            q -= 1
        op, a, b, c = ins[q][:4]
        if op in TESTS or op in (OP['JMP'], OP['RETURN'], OP['TAILCALL'], OP['FORLOOP'],
                                 OP['FORPREP'], OP['TFORLOOP'], OP['CLOSE'], OP['SETGLOBAL'],
                                 OP['SETUPVAL']):
            break
        if op == OP['CALL'] and c == 1 or op == OP['SETTABLE'] and a < r:
            break
        if code.writes[q] & ((1 << r) - 1):
            break
        if op == OP['LOADBOOL'] and c:
            break
        s = q
        if q in targets:
            break
    return s


def chain_tests(code, first, r):
    """Value tests (TEST/TESTSET writing r, value live at the jump target) of the chain that
    starts at `first`, and the chain's join pc (the furthest target, reached before any test
    beyond it)."""
    ins = code.ins
    tests = []
    join = None
    for q in range(first, code.n - 1):
        if join is not None and q >= join:
            break
        t = code.value_test(q)
        if t is not None and ins[q][1] == r:
            tests.append(q)
            join = max(join or 0, t)
    return tests, join


def rewrite(proto, code, first, r):
    """Rewrite the chain whose first value test on r is at `first`. Returns the highest register
    used afterwards (for maxstack)."""
    ins = code.ins
    tests, join = chain_tests(code, first, r)
    start = chain_start(code, first, r)
    top = 0
    begin = start
    for t in tests:
        for q in range(begin, t):
            proto.code[q] = _shift_pseudo(proto.code[q], r) if code.pseudo[q] else _shift(proto.code[q], r)
            top = max(top, _max_reg(proto.code[q], code.pseudo[q]))
        op, a, b, c = ins[t][:4]
        src = r + 1 if op == OP['TEST'] else (b + 1 if b >= r else b)
        proto.code[t] = _abc(OP['TESTSET'], r, src, c)
        top = max(top, src)
        begin = t + 2
    # final operand: luac computes it into r directly only when it is a single instruction; a
    # longer computation (a call, a field chain) goes to r+1 and is moved into r
    end = begin
    while end < join and not (not code.pseudo[end] and (ins[end][0] in TESTS or ins[end][0] == OP['JMP']
                                                         or ins[end][0] == OP['LOADBOOL'] and ins[end][3])):
        end += 1
    real = [q for q in range(begin, end) if not code.pseudo[q]]
    if len(real) > 1 and code.writes[real[-1]] >> r & 1:
        for q in range(begin, end):
            proto.code[q] = _shift_pseudo(proto.code[q], r) if code.pseudo[q] else _shift(proto.code[q], r)
            top = max(top, _max_reg(proto.code[q], code.pseudo[q]))
        insert_word(proto, end, _abc(OP['MOVE'], r, r + 1, 0))
        join += 1
    # r is a temp of the enclosing expression while the chain computes it; local inference must
    # not read the rewritten TESTSET r r+1 / MOVE r r+1 (luac emits those only for locals) as
    # declaring r
    proto.chain_marks = getattr(proto, 'chain_marks', []) + [(start, join, r)]
    return top


def insert_word(proto, at, word, entry=False):
    """Insert one instruction before pc `at`, relocating jumps. Jumps to `at` reach the old
    instruction there (skipping the new one) unless entry is set, in which case they reach the
    new instruction (it becomes the first instruction of the statement)."""
    ins = [decode(w) for w in proto.code]
    pseudo = [False] * len(ins)
    for pc, (op, a, b, c, bx, sbx) in enumerate(ins):
        if pseudo[pc]:
            continue
        if op == OP['CLOSURE']:
            for k in range(proto.protos[bx].nups):
                pseudo[pc + 1 + k] = True
        elif op == OP['SETLIST'] and c == 0:
            pseudo[pc + 1] = True
    new = []
    for pc, w in enumerate(proto.code):
        if pc == at:
            new.append(word)
        op = ins[pc][0]
        if not pseudo[pc] and op in (OP['JMP'], OP['FORLOOP'], OP['FORPREP']):
            dst = pc + 1 + ins[pc][5]
            npc = pc + (1 if pc >= at else 0)
            # a jump to `at` reaches the old instruction there, not the inserted MOVE
            ndst = dst + (1 if dst > at or dst == at and not entry else 0)
            w = (w & 0x3FFF) | ((ndst - (npc + 1) + 131071) << 14)
        new.append(w)
    proto.code = new
    shift_marks(proto, at, 1)
    if proto.lineinfo:
        proto.lineinfo = proto.lineinfo[:at] + [proto.lineinfo[min(at, len(proto.lineinfo) - 1)]] + proto.lineinfo[at:]


def shift_marks(proto, at, k):
    """Relocate chain marks for k instructions inserted before pc `at` (the inserted ones fall
    outside every marked range)."""
    if getattr(proto, 'chain_marks', None):
        proto.chain_marks = [(s + k * (at <= s), j + k * (at < j), r) for s, j, r in proto.chain_marks]


def _max_reg(word, pseudo):
    op, a, b, c, bx, sbx = decode(word)
    if pseudo:
        return b if op == OP['MOVE'] else 0
    m = a if op in REG_A else 0
    if op in REG_B or op in RK_B and b < 256:
        m = max(m, b)
    if op in REG_C or op in RK_C and c < 256:
        m = max(m, c)
    if op in (OP['CALL'], OP['TAILCALL']):
        m = max(m, a + max(b, c) + 1)
    if op == OP['SELF']:
        m = max(m, a + 1)
    return m


def rewrite_all(main):
    """Rewrite every mixed value chain in every function. Returns the number rewritten."""
    from locinfer import Code
    count = 0
    stack = [main]
    while stack:
        p = stack.pop()
        stack.extend(p.protos)
        for _ in range(200):
            code = Code(p)
            code.liveness()
            todo = [(f, r) for f, r in code.mixed_chains().items() if not _rewritten(code, f, r)]
            if not todo:
                break
            first, r = max(todo)
            top = rewrite(p, code, first, r)
            p.maxstack = min(250, max(p.maxstack, top + 2))
            count += 1
    return count


def _rewritten(code, first, r):
    """True when no value test of the chain is still a TEST on r itself (TESTSET with another
    source register, a local or r+1, is the form unluac handles)."""
    tests, _ = chain_tests(code, first, r)
    return all(code.ins[t][0] == OP['TESTSET'] for t in tests)
