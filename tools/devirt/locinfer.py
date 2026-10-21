"""Infer local-variable debug info (locvars) for stripped Lua 5.1 bytecode produced by luac 5.1.

See locinfer_pseudo.txt for the rules. Entry point: infer(proto) -> [(name, startpc, endpc)].
"""
from luac51 import OP, decode

ALL = (1 << 256) - 1
TESTS = {OP['EQ'], OP['LT'], OP['LE'], OP['TEST'], OP['TESTSET']}
STATEMENT_OPS = {OP['SETGLOBAL'], OP['SETUPVAL'], OP['RETURN'], OP['TAILCALL'], OP['FORLOOP'],
                 OP['FORPREP'], OP['TFORLOOP'], OP['CLOSE'], OP['SETLIST']}
# instructions whose A is a result computed from register operands (temps are allocated upward
# and a result reuses the lowest freed temp, so operand/target order identifies locals)
VALUE_OPS = {OP['MOVE'], OP['GETTABLE'], OP['SELF'], OP['UNM'], OP['NOT'], OP['LEN'],
             OP['CONCAT'], OP['TESTSET'], OP['SETTABLE']} | set(range(OP['ADD'], OP['POW'] + 1))


def _rk(x):
    """Register mask for an RK operand (constants have bit 8 set)."""
    return 0 if x & 256 else 1 << x


def _span(lo, hi):
    """Mask of registers lo..hi inclusive (empty when hi < lo)."""
    return ((1 << (hi - lo + 1)) - 1) << lo if hi >= lo else 0


class Code:
    """Decoded instructions plus per-pc effects."""

    def __init__(self, proto):
        self.proto = proto
        self.ins = [decode(w) for w in proto.code]
        n = len(self.ins)
        self.n = n
        self.pseudo = [False] * n
        for pc, (op, a, b, c, bx, sbx) in enumerate(self.ins):
            if self.pseudo[pc]:
                continue
            if op == OP['CLOSURE']:
                for k in range(proto.protos[bx].nups):
                    self.pseudo[pc + 1 + k] = True
            elif op == OP['SETLIST'] and c == 0:
                self.pseudo[pc + 1] = True
        self.reads = [0] * n      # registers read
        self.kills = [0] * n      # registers definitely written
        self.writes = [0] * n     # registers possibly written (kills + conditional writes)
        self.succ = [()] * n
        self.target = [None] * n  # result register of value-producing instructions
        self.edge_kills = {}      # pc -> {successor: registers written only on that edge}
        self.reserved = [0] * n   # temp destinations of chains rewritten by chains.py (liveness)
        for pc in range(n):
            if not self.pseudo[pc]:
                self._effects(pc)
        self.reachable = set()
        stack = [0]
        while stack:
            pc = stack.pop()
            if pc < n and pc not in self.reachable:
                self.reachable.add(pc)
                stack.extend(self.succ[pc])

    def _multret_top(self, pc, base):
        """Registers base..producer.A read by a B==0 consumer; the producer is the previous
        instruction (CALL C==0 or VARARG B==0) and writes producer.A upward."""
        op, a = self.ins[pc - 1][:2]
        assert op in (OP['CALL'], OP['VARARG'], OP['TAILCALL']), pc
        return _span(base, a)

    def _effects(self, pc):
        op, a, b, c, bx, sbx = self.ins[pc]
        r = w = 0
        succ = (pc + 1,)
        if op == OP['MOVE']:
            r, w = 1 << b, 1 << a
        elif op in (OP['LOADK'], OP['GETGLOBAL'], OP['GETUPVAL'], OP['NEWTABLE']):
            w = 1 << a
        elif op == OP['LOADBOOL']:
            w = 1 << a
            if c:
                succ = (pc + 2,)
        elif op == OP['LOADNIL']:
            w = _span(a, b)
        elif op == OP['GETTABLE']:
            r, w = (1 << b) | _rk(c), 1 << a
        elif op in (OP['SETGLOBAL'], OP['SETUPVAL']):
            r = 1 << a
        elif op == OP['SETTABLE']:
            r = (1 << a) | _rk(b) | _rk(c)
        elif op == OP['SELF']:
            r, w = (1 << b) | _rk(c), _span(a, a + 1)
        elif OP['ADD'] <= op <= OP['POW']:
            r, w = _rk(b) | _rk(c), 1 << a
        elif op in (OP['UNM'], OP['NOT'], OP['LEN']):
            r, w = 1 << b, 1 << a
        elif op == OP['CONCAT']:
            r, w = _span(b, c), 1 << a
        elif op == OP['JMP']:
            succ = (pc + 1 + sbx,)
        elif op in (OP['EQ'], OP['LT'], OP['LE']):
            r, succ = _rk(b) | _rk(c), (pc + 1, pc + 2)
        elif op == OP['TEST']:
            r, succ = 1 << a, (pc + 1, pc + 2)
        elif op == OP['TESTSET']:
            # writes A only on the path that takes the following JMP: a possible write, no kill
            r, succ = 1 << b, (pc + 1, pc + 2)
            self.writes[pc] = 1 << a
            self.edge_kills[pc] = {pc + 1: 1 << a}
        elif op in (OP['CALL'], OP['TAILCALL']):
            r = _span(a, a + b - 1) if b else (1 << a) | self._multret_top(pc, a + 1)
            if op == OP['CALL']:
                w = _span(a, a + c - 2) if c else ALL & ~((1 << a) - 1)
        elif op == OP['RETURN']:
            r = _span(a, a + b - 2) if b else self._multret_top(pc, a)
            succ = ()
        elif op == OP['FORLOOP']:
            # the loop variable A+3 is written only when the loop continues
            r, w = _span(a, a + 2), 1 << a
            self.writes[pc] = 1 << (a + 3)
            succ = (pc + 1 + sbx, pc + 1)
            self.edge_kills[pc] = {pc + 1 + sbx: 1 << (a + 3)}
        elif op == OP['FORPREP']:
            r, w = _span(a, a + 2), 1 << a
            succ = (pc + 1 + sbx,)
        elif op == OP['TFORLOOP']:
            r, w = _span(a, a + 2), _span(a + 3, a + 2 + c)
            self.writes[pc] = 1 << (a + 2)
            succ = (pc + 1, pc + 2)
        elif op == OP['SETLIST']:
            r = _span(a, a + b) if b else (1 << a) | self._multret_top(pc, a + 1)
            if c == 0:
                succ = (pc + 2,)
        elif op == OP['CLOSURE']:
            w = 1 << a
            nups = self.proto.protos[bx].nups
            for k in range(nups):
                pop, _, pb = self.ins[pc + 1 + k][:3]
                if pop == OP['MOVE'] and pb != a:   # a local function captures its own
                    r |= 1 << pb                     # register, which CLOSURE writes
            succ = (pc + 1 + nups,)
        elif op == OP['VARARG']:
            w = _span(a, a + b - 2) if b else ALL & ~((1 << a) - 1)
        # CLOSE: no register effects
        if op in VALUE_OPS:
            self.target[pc] = a
        self.reads[pc] = r
        self.kills[pc] = w
        self.writes[pc] |= w
        self.succ[pc] = tuple(s for s in succ if s < self.n)

    def liveness(self):
        """live_in[pc]: registers whose current value may be read later (before a definite write)."""
        n = self.n
        live = [0] * n
        order = [pc for pc in range(n - 1, -1, -1) if not self.pseudo[pc]]
        changed = True
        while changed:
            changed = False
            for pc in order:
                ek = self.edge_kills.get(pc)
                out = 0
                for s in self.succ[pc]:
                    out |= live[s] & ~ek.get(s, 0) if ek else live[s]
                v = self.reads[pc] | (out & ~self.kills[pc])
                if v != live[pc]:
                    live[pc] = v
                    changed = True
        self.live = live
        # registers of rewritten chains (chains.py): the destination r is not written until the
        # first TESTSET r r+1, so liveness alone would read it as a dead local
        for s0, j, r in getattr(self.proto, 'chain_marks', ()):
            for pc in range(s0, min(j, n)):
                self.reserved[pc] |= 1 << r
        return live

    def live_out(self, pc):
        ek = self.edge_kills.get(pc)
        out = 0
        for s in self.succ[pc]:
            out |= self.live[s] & ~ek.get(s, 0) if ek else self.live[s]
        return out

    def _uncond_jmp(self, pc):
        """True when pc holds a JMP that is not the jump half of a test."""
        if pc <= 0 or pc >= self.n or self.pseudo[pc] or self.ins[pc][0] != OP['JMP']:
            return False
        return self.pseudo[pc - 1] or self.ins[pc - 1][0] not in TESTS

    def regions(self):
        """Scope intervals [start, end). A local declared at s ends at the smallest end of an
        interval containing s whose end leaves its register dead (see scope_end)."""
        regs = []
        loop_end = {}             # loop head -> last backward JMP (continue/escape JMPs also go there)
        self.tfor_prep = {}       # TFORLOOP pc -> prep JMP pc
        self.tfor_prep_pcs = set()
        conds = []
        self.cond_regions = []
        for pc in range(self.n):
            if self.pseudo[pc]:
                continue
            op, a, b, c, bx, sbx = self.ins[pc]
            if op == OP['JMP']:
                t = pc + 1 + sbx
                cond = not self._uncond_jmp(pc)
                if t <= pc:
                    loop_end[t] = max(loop_end.get(t, 0), pc + 1 if cond else pc)
                elif cond:
                    conds.append((pc, t))
            elif op == OP['FORPREP']:
                q = pc + 1 + sbx
                regs.append((pc + 1, q))
                regs.append((pc, q + 1))
            elif op == OP['TFORLOOP']:
                # the JMP after TFORLOOP goes back to the body start, right after the prep JMP
                # (other forward JMPs may also target the TFORLOOP: escapes at the end of the body)
                prep = pc + 1 + self.ins[pc + 1][5]
                assert self.ins[prep][0] == OP['JMP'] and prep + 1 + self.ins[prep][5] == pc, pc
                self.tfor_prep[pc] = prep
                self.tfor_prep_pcs.add(prep)
        for pc, t in conds:
            end = t
            e = t + self.ins[t - 1][5] if t - 1 > pc and self._uncond_jmp(t - 1) else None
            # an else-skip JMP goes forward past the else block, or back to the head of an
            # enclosing loop (luac retargets it when the if statement ends a loop body); a
            # backward JMP into the then-block, or the JMP after TFORLOOP, closes an inner loop
            if e is not None and (e >= t or e <= pc) and self.ins[t - 2][0] != OP['TFORLOOP']:
                end = t - 1
                else_end = e if e >= t else loop_end.get(e, t)
                regs.append((t, else_end))
                self.cond_regions.append((t, else_end, pc))      # else-block of the same if
            regs.append((pc + 1, end))
            self.cond_regions.append((pc + 1, t, pc))
        for q, p in self.tfor_prep.items():
            regs.append((p + 1, q))
            regs.append((p, q + 2))
        self.loops = sorted(loop_end.items())
        for head, end in loop_end.items():
            regs.append((head + 1, end))    # a local starting at the head was declared before the loop
        self.regs = regs
        return regs

    def scope_end(self, s, reg):
        """Smallest enclosing region end at which reg is no longer live (default: function end)."""
        ends = sorted(end for start, end in self.regs if start <= s < end)
        for end in ends:
            if end >= self.n - 1 or not (self.live[end] >> reg) & 1:
                return min(end, self.n - 1)
        return self.n - 1

    def _statement_end(self, end, start):
        """Move an implicit scope end out of any if statement or loop that began at or after the
        local's start and is still running at end (a do-block cannot close mid-statement)."""
        changed = True
        while changed:
            changed = False
            for rs, re_, jmp in self.cond_regions:
                if start <= jmp - 1 < end < re_:
                    end = re_
                    changed = True
            for head, back in self.loops:
                if start <= head < end <= back:
                    end = back + 1
                    changed = True
        return end

    def _prev(self, pc):
        """Previous real (non-pseudo) instruction before pc, or -1."""
        pc -= 1
        while pc >= 0 and self.pseudo[pc]:
            pc -= 1
        return pc

    def _last_touch(self, reg, lo, hi, mask_name):
        """Largest pc in [lo, hi) whose `mask_name` set contains reg, or -1."""
        masks = getattr(self, mask_name)
        bit = 1 << reg
        for pc in range(hi - 1, lo - 1, -1):
            if not self.pseudo[pc] and masks[pc] & bit:
                return pc
        return -1

    def _decl_write(self, reg, pc, floor):
        """The write that initialises a local in reg found to be live at pc. Usually the last
        write before pc; when that write sits in a conditional block that ended before pc, the
        local existed before the if statement (`local x = v; if c then x = w end`), so use the
        last write before the condition when it is a clean initialiser (not cut off by floor)."""
        last = self._last_touch(reg, 0, pc, 'writes')
        w = last
        bit = 1 << reg
        while w >= 0:
            # a value that flows around a loop's back edge belongs to a local declared before it
            head = None
            for h, e in self.loops:
                if h <= w < e and self.live[h] & bit and (head is None or h < head):
                    head = h
            if head is not None:
                w = self._last_touch(reg, 0, head, 'writes')
                last = w if w + 1 >= floor else -1
                continue
            # (a value still live where the block rejoins also outlives a local of the block, even
            # when pc lies inside it: `local x = a; if c then x = b else x = d; f() end; g(x)`)
            cj = None
            for start, end, jmp in self.cond_regions:
                if (start <= w < end and (end <= pc or end < self.n and self.live[end] & bit)
                        and (cj is None or jmp < cj[2])):
                    cj = (start, end, jmp)
            if cj is None or self._expr_region(reg, *cj):
                break
            w = self._last_touch(reg, 0, cj[2] - 1, 'writes')
            if w >= 0 and self.ins[w][0] == OP['TESTSET']:
                return last
        if w != last and (w < 0 or w + 1 < floor):
            return last
        return w

    STMT_ONLY = {OP['SETGLOBAL'], OP['SETUPVAL'], OP['RETURN'], OP['TAILCALL'], OP['CLOSE'],
                 OP['FORPREP'], OP['FORLOOP'], OP['TFORLOOP']}

    def _expr_region(self, reg, start, end, jmp):
        """True when the conditional block [start, end) controlled by the jump at jmp is part of
        an and/or (or comparison) expression computing reg, rather than an if statement: the
        test is on reg itself (or the block uses the LOADBOOL skip pair of a comparison value),
        and the block holds no statement-only instruction and writes no register below reg."""
        top, ta = self.ins[jmp - 1][0], self.ins[jmp - 1][1]
        on_reg = top in (OP['TEST'], OP['TESTSET']) and ta == reg
        below = (1 << reg) - 1
        for q in range(start, end):
            if self.pseudo[q]:
                continue
            op, a, b, c = self.ins[q][:4]
            if op == OP['LOADBOOL'] and a == reg and c and q in self.reachable:
                on_reg = True
            if op in self.STMT_ONLY or op == OP['CALL'] and c == 1 or self.writes[q] & below:
                return False
            if op == OP['SETTABLE'] and a < reg:
                return False
            if op == OP['JMP'] and self.ins[q][5] < 0:
                return False
        return on_reg

    def constraints(self, pc, nact):
        """(exact, upper, lower) bounds on the number of active locals at pc, given the
        current count nact. None means no bound of that kind."""
        op, a, b, c, bx, sbx = self.ins[pc]
        exact = upper = None
        lower = 0
        self.why = None
        if op == OP['CALL']:
            if c == 1:
                exact = a
            else:
                upper = a
        elif op == OP['TAILCALL']:
            exact = a
        elif op == OP['RETURN']:
            if b == 0 or b >= 3:
                exact = a
            elif b == 2:
                if a > lower:
                    self.why = 'return1'
                lower = a
        elif op in (OP['SELF'], OP['SETLIST']):
            upper = a
        elif op == OP['VARARG'] and b != 2:
            upper = a
        elif op == OP['CONCAT']:
            upper = b
        elif op == OP['FORPREP']:
            exact = a
        elif op == OP['JMP'] and pc in self.tfor_prep_pcs:
            exact = self.ins[pc + 1 + sbx][1]
        elif op == OP['CLOSURE']:
            for k in range(self.proto.protos[bx].nups):
                pop, _, pb = self.ins[pc + 1 + k][:3]
                if pop == OP['MOVE'] and pb != a:
                    if max(lower, pb + 1) > lower:
                        self.why = 'capture'
                    lower = max(lower, pb + 1)
        t = self.target[pc]
        if t is not None:
            ops = self.reads[pc] & ~(1 << t) if op == OP['SETTABLE'] else self.reads[pc]
            below = ops & ((1 << t) - 1)
            if below:
                if max(lower, below.bit_length()) > lower:
                    self.why = 'operand-below'
                lower = max(lower, below.bit_length())
            # the result reuses the lowest freed temp, so a target that is not one of its own
            # operands yet sits below an operand can only be a local
            # (except the destination of a chain rewritten by chains.py, which may be a temp)
            # (SELF A B C reads its method name from A+2 when the constant index exceeds 255)
            if (op != OP['SETTABLE'] and ops >> (t + 1) and not (ops >> t) & 1
                    and not (self.reserved[pc] >> t) & 1
                    and not (op == OP['SELF'] and ops >> (t + 1) == 2)):
                if max(lower, t + 1) > lower:
                    self.why = 'target-below'
                lower = max(lower, t + 1)
        # temps form a contiguous run of pending (live) values from nact upward; an instruction
        # that writes a fresh target above a dead register in that run means the dead register
        # is a local (declared without an initialiser, or no longer used)
        w = self.kills[pc] | self.writes[pc]
        if w and op not in (OP['FORLOOP'], OP['TFORLOOP'], OP['FORPREP']):
            low = (w & -w).bit_length() - 1          # lowest written register
            if low > nact:
                gap = _span(nact, low - 1) & ~self.live[pc] & ~self.reserved[pc]
                # slots reserved for the next instruction (SELF reserves A, A+1 before loading a
                # method-name constant above 255 into A+2) are temps, not dead locals
                nxt = pc + 1
                while nxt < self.n and self.pseudo[nxt]:
                    nxt += 1
                if nxt < self.n:
                    gap &= ~(self.kills[nxt] & ~self.reads[nxt])
                if gap:
                    if max(lower, gap.bit_length()) > lower:
                        self.why = 'gap'
                    lower = max(lower, gap.bit_length())
        # `if v then ... v ... end`: a tested value that is still needed in the then-block but not at
        # the jump target is a local (a temp condition is dead on both paths; an and/or value
        # expression carries it to the jump target)
        if op == OP['TEST'] and pc + 2 < self.n and self.ins[pc + 1][0] == OP['JMP']:
            tgt = pc + 2 + self.ins[pc + 1][5]
            if (self.live[pc + 2] >> a) & 1 and tgt < self.n and not (self.live[tgt] >> a) & 1:
                if a + 1 > lower:
                    self.why = 'test-reuse'
                lower = max(lower, a + 1)
        # a value read here and read again later is a local (temps are read once; TEST excluded)
        if op != OP['TEST']:
            out = self.live_out(pc)
            reads = self.reads[pc]
            if op in (OP['SETTABLE'], OP['SETLIST']):
                # a table under construction (last written by NEWTABLE) is read without being
                # consumed; any other object stored into repeatedly is held in a local
                w = self._last_touch(a, 0, pc, 'writes')
                if w >= 0 and self.ins[w][0] == OP['NEWTABLE']:
                    reads &= ~(1 << a)
            multi = reads & out & ~self.kills[pc] & ~((1 << nact) - 1)
            if multi:
                if max(lower, multi.bit_length()) > lower:
                    self.why = 'multi-read'
                lower = max(lower, multi.bit_length())
        # statement boundary: every live register is a local
        if self._certain_boundary(pc, nact):
            live = self.live[pc]
            if live:
                if max(lower, live.bit_length()) > lower:
                    self.why = 'boundary'
                lower = max(lower, live.bit_length())
        return exact, upper, lower

    def _certain_boundary(self, pc, nact):
        """True when the previous instruction certainly ended a statement."""
        if pc == 0:
            return True
        p = self._prev(pc)
        if p < 0:
            return True
        op, a, b, c, bx, sbx = self.ins[p]
        if op == OP['CALL']:
            return c == 1
        if op in (OP['RETURN'], OP['FORPREP'], OP['FORLOOP'], OP['CLOSE']):
            return True
        # a store from a temp while other temps are still live is the middle of a multiple
        # assignment (a, b = x, y stores the values in reverse order), not a statement end
        stored = {OP['MOVE']: b, OP['SETGLOBAL']: a, OP['SETUPVAL']: a,
                  OP['SETTABLE']: c if c < 256 else -1}.get(op)
        if stored is not None and stored >= nact and self.live[pc] >> nact:
            return False
        if op in (OP['SETGLOBAL'], OP['SETUPVAL']):
            return True
        if op == OP['SETTABLE']:
            return a < nact or not (self.live[pc] >> a) & 1
        if op == OP['JMP']:
            # the JMP after TFORLOOP leads to the loop exit
            q = self._prev(p)
            if q >= 0 and self.ins[q][0] == OP['TFORLOOP']:
                return True
            # after an unconditional JMP (a break, an else skip) only jumps reach pc: a statement
            # start unless temps are pending or pc is the LOADBOOL pair of a comparison value
            return (q < 0 or self.ins[q][0] not in TESTS) and self.ins[pc][0] != OP['LOADBOOL'] \
                and not self.live[pc] >> nact
        if op in TESTS or op == OP['LOADBOOL'] and c:
            return False
        w = self.kills[p]
        return bool(w) and w >> nact == 0 and op not in (OP['TAILCALL'], OP['VARARG'])

    def value_test(self, pc):
        """Jump target of the and/or value test at pc, else None: TEST/TESTSET A + JMP whose
        tested value is live at the target; a TEST's value must be computed into A by the
        instruction before it (otherwise it tests an existing local: `if not x then`)."""
        if self.pseudo[pc] or pc + 1 >= self.n:
            return None
        op, a = self.ins[pc][:2]
        if op not in (OP['TEST'], OP['TESTSET']) or self.ins[pc + 1][0] != OP['JMP']:
            return None
        t = pc + 2 + self.ins[pc + 1][5]
        if t >= self.n or not (self.live[t] >> a) & 1:
            return None
        if op == OP['TEST']:
            q = pc - 1
            while q >= 0 and self.pseudo[q]:
                q -= 1
            if q < 0 or not (self.writes[q] >> a) & 1:
                return None
        return t

    def mixed_chains(self):
        """{first test pc: register} for and/or value expressions that unluac mis-decompiles when
        the result goes to a temp: value tests (TEST/TESTSET writing r, value live at the jump
        target) of both polarities that reach the same join point (rewritten by chains.py)."""
        groups = {}
        for pc in range(self.n - 1):
            t = self.value_test(pc)
            if t is not None:
                a, c = self.ins[pc][1], self.ins[pc][3]
                groups.setdefault((a, t), []).append((pc, c))
        out = {}
        for (r, t), tests in groups.items():
            if len({c for _, c in tests}) > 1:
                first = min(pc for pc, _ in tests)
                # the chain starts at its first value test on r, even one aimed at an inner join;
                # a statement or a plain condition test on r ends the expression
                for pc in range(first - 1, -1, -1):
                    if self.pseudo[pc]:
                        continue
                    op, a, b, c, bx, sbx = self.ins[pc]
                    if op in (OP['TEST'], OP['TESTSET']) and a == r and pc + 1 < first:
                        q = self.value_test(pc)
                        if q is None or not pc < q <= t:
                            break
                        first = pc
                    elif op == OP['JMP'] and not (pc > 0 and self.ins[pc - 1][0] in TESTS):
                        break
                    elif op in STATEMENT_OPS or op == OP['CALL'] and c == 1 \
                            or op == OP['SETTABLE'] and a < r:
                        break
                    elif self.kills[pc] & ~((1 << r) - 1) == 0 and self.kills[pc]:
                        break
                out[first] = r
        return out

    def walk(self):
        """Forward pass building declarations. Returns [[reg, startpc, endpc, kind]] in
        declaration order; kind is 'param', 'local', 'forctl', 'forvar' or 'func'."""
        self.liveness()
        self.regions()
        tfor_preps = {p: q for q, p in self.tfor_prep.items()}
        self.conflicts = 0
        decls = []
        active = []               # indices into decls; position == register
        floor = [0] * 256         # earliest startpc allowed per register (after its last forced end)

        child_end = [0] * 257     # per stack slot: latest end of a local that sat above it
        last_decl = {}            # register -> index of its most recent declaration

        needed = {}               # decl index -> last pc whose constraints required it

        def push(reg, start, end, kind, why=None):
            assert reg == len(active), (reg, len(active))
            decls.append([reg, start, end, kind, why, floor[reg]])
            active.append(len(decls) - 1)
            last_decl[reg] = len(decls) - 1
            child_end[reg] = 0

        def pop_to(count, pc, implicit):
            """End locals above count. implicit: end right after the last use (do-block).
            A local never ends before a local declared above it (scopes nest)."""
            while len(active) > count:
                idx = active.pop()
                d = decls[idx]
                end = pc
                if implicit:
                    # a write after the last read starts a new value (a temp of a later
                    # statement), so the old local ends after its last read
                    # the register is reused by the statement containing pc, so the old local
                    # ends by that statement's start: the latest certain boundary before pc
                    b = pc
                    # (the local being ended, and those above it, no longer count as active)
                    while b > d[1] and not self._certain_boundary(b, d[0]):
                        b -= 1
                    last = self._last_touch(d[0], d[1], b, 'reads')
                    # end at the first statement boundary after the last read (not mid-statement)
                    # and after the last instruction whose register layout needed the local
                    e = max(d[1], last + 1, needed.get(idx, -1) + 1)
                    while e < b and not self._certain_boundary(e, d[0]):
                        e += 1
                    end = self._statement_end(max(e, child_end[d[0]]), d[1])
                    end = min(pc, max(end, child_end[d[0]]) if end <= b else b)
                d[2] = end
                if d[0] > 0:
                    child_end[d[0] - 1] = max(child_end[d[0] - 1], end)
                floor[d[0]] = max(floor[d[0]], end + 1)   # unluac treats endpc as inclusive

        def reopen(reg, pc):
            """The previous local in reg ended at pc but reg is still needed: extend it."""
            i = last_decl[reg]
            d = decls[i]
            d[2] = max(self.scope_end(pc, reg), pc + 1)
            active.append(i)
            floor[reg] = d[5]

        for r in range(self.proto.numparams):
            push(r, 0, self.n - 1, 'param')
        for pc in range(self.n):
            if self.pseudo[pc]:
                continue
            op, a, b, c, bx, sbx = self.ins[pc]
            # scope ends (keep the stack discipline: ending a local ends everything above it)
            for i, di in enumerate(active):
                if decls[di][2] <= pc:
                    pop_to(i, pc, False)
                    break
            if op == OP['CLOSE'] and not self._uncond_jmp(pc + 1):
                pop_to(min(a, len(active)), pc, False)
            exact, upper, lower = self.constraints(pc, len(active))
            hi = min(x for x in (exact, upper, 256) if x is not None)
            lo = exact if exact is not None else min(lower, hi)
            if exact is None and lower > hi:
                self.conflicts += 1
            if len(active) > hi:
                pop_to(hi, pc, True)
            if len(active) < lo:
                base = len(active)
                # results of one multi-result CALL/VARARG form one `local a, b = f()`: declaring
                # one of them declares all of them
                grow = True
                while grow:
                    grow = False
                    for reg in range(base, lo):
                        w = self._last_touch(reg, 0, pc, 'writes')
                        if w < 0:
                            continue
                        wop, wa, wb, wc = self.ins[w][:4]
                        top = (wa + wc - 2 if wop == OP['CALL'] and wc >= 3 else
                               wa + wb - 2 if wop == OP['VARARG'] and wb >= 3 else -1)
                        if top >= lo and (hi == 256 or top < hi):
                            lo = top + 1
                            grow = True
                starts = [max(self._decl_write(reg, pc, floor[reg]) + 1, floor[reg])
                          for reg in range(base, lo)]
                # a lower register cannot start after a higher one (stack order)
                for i in range(len(starts) - 2, -1, -1):
                    starts[i] = max(min(starts[i], starts[i + 1]), floor[base + i])
                # a new local that must start earlier pulls the active locals below it earlier
                s0 = starts[0]
                for idx in reversed(active):
                    d = decls[idx]
                    if d[1] <= s0 or d[3] != 'local' or d[5] > s0:
                        break
                    d[1] = s0
                    d[2] = max(d[2], self.scope_end(s0, d[0]))
                prev = 0
                for idx in active:          # starts stay non-decreasing up the stack
                    d = decls[idx]
                    if d[1] < prev:
                        d[1] = prev
                    prev = d[1]
                low = decls[active[-1]][1] if active else 0
                for i, reg in enumerate(range(base, lo)):
                    if floor[reg] > pc and reg in last_decl and decls[last_decl[reg]][3] == 'local':
                        reopen(reg, pc)
                        low = decls[active[-1]][1]
                        continue
                    start = min(max(starts[i], low), pc)
                    while self.pseudo[start]:
                        start += 1
                    low = start
                    push(reg, start, self.scope_end(start, reg), 'local', (pc, self.why))
            for idx in active[:lo]:
                needed[idx] = pc
            # registers at or above hi are not locals here: later declarations start after pc
            for reg in range(hi, self.proto.maxstack + 1):
                floor[reg] = max(floor[reg], pc + 1)
            # declarations created by this instruction
            if op == OP['FORPREP']:
                q = pc + 1 + sbx
                for k in range(3):
                    push(a + k, pc, q + 1, 'forctl')
                push(a + 3, pc + 1, q, 'forvar')
            elif pc in tfor_preps:
                q = tfor_preps[pc]
                ta, tc = self.ins[q][1], self.ins[q][3]
                for k in range(3):
                    push(ta + k, pc, q + 2, 'forctl')
                for k in range(tc):
                    push(ta + 3 + k, pc + 1, q, 'forvar')
            elif op == OP['CLOSURE'] and a == len(active):
                nups = self.proto.protos[bx].nups
                selfcap = any(self.ins[pc + 1 + k][0] == OP['MOVE'] and self.ins[pc + 1 + k][2] == a
                              for k in range(nups))
                if selfcap:
                    start = pc + 1 + nups
                    push(a, start, self.scope_end(start, a), 'func')
        pop_to(0, self.n - 1, False)
        return decls


FORCTL = (('(for index)', '(for limit)', '(for step)'),
          ('(for generator)', '(for state)', '(for control)'))


def _name(reg, kind, depth):
    return f'A{reg}_{depth}' if kind == 'param' else f'L{reg}_{depth}'


def infer(proto, depth=1, upnames=()):
    """Fill proto.locvars and proto.upvalnames for proto and all nested functions."""
    code = Code(proto)
    decls = code.walk()
    names = []
    for reg, start, end, kind in (d[:4] for d in decls):
        if kind == 'forctl':
            # numeric for: FORPREP at start; generic for: JMP at start
            numeric = code.ins[start][0] == OP['FORPREP']
            name = FORCTL[0 if numeric else 1][(reg - _forbase(decls, reg, start)) % 3]
        else:
            name = _name(reg, kind, depth)
        names.append(name)
    proto.locvars = [(n.encode(), d[1], d[2]) for n, d in zip(names, decls)]
    proto.upvalnames = [u.encode() if isinstance(u, str) else u for u in upnames]
    # unluac treats a function with neither line info nor locals as stripped and falls back to
    # register-level output, so give every function a (placeholder) line table
    if not proto.lineinfo:
        proto.lineinfo = [1] * len(proto.code)
    for pc in range(code.n):
        if code.pseudo[pc] or code.ins[pc][0] != OP['CLOSURE']:
            continue
        child = proto.protos[code.ins[pc][4]]
        child_up = []
        for k in range(child.nups):
            pop, _, pb = code.ins[pc + 1 + k][:3]
            if pop == OP['GETUPVAL']:
                child_up.append(proto.upvalnames[pb])
            else:
                selfstart = pc + 1 + child.nups if pb == code.ins[pc][1] else None
                child_up.append(_local_at(decls, names, pb, pc, selfstart))
        infer(child, depth + 1, child_up)
    return proto


def _forbase(decls, reg, start):
    """Register of the first control variable of the for loop that declares reg at start."""
    base = reg
    for r, s, e, kind in (d[:4] for d in decls):
        if kind == 'forctl' and s == start:
            base = min(base, r)
    return base


def _local_at(decls, names, reg, pc, selfstart):
    """Name of the local in register reg at pc. A local function captures its own register
    before its declaration starts (selfstart = its startpc)."""
    best = None
    for i, (r, s, e, kind) in enumerate(d[:4] for d in decls):
        if r == reg and (s <= pc < e or s == selfstart):
            best = i
    if best is None:
        return f'U{reg}'.encode()
    return names[best].encode()


def unluac_registers(decls, n):
    """Registers unluac assigns: in list order, the first register free at line `start`, then
    lines start..end (inclusive) are occupied. Returns [(decl index, intended, assigned)] for
    every declaration whose assigned register differs from the intended one."""
    occ = {}
    bad = []
    for i, d in enumerate(decls):
        reg, start, end = d[0], d[1], d[2]
        r = 0
        while (r, start) in occ:
            r += 1
        for line in range(start, end + 1):
            occ[(r, line)] = i
        if r != reg:
            bad.append((i, reg, r))
    return bad
