"""Control-flow equivalence of two chunks, function by function (preorder).

Each function becomes a graph of non-jump instructions. Unconditional jumps are followed to their
final target; a test becomes a node with ordered (condition true, condition false) successors, so
test polarity and block layout do not matter. NEWTABLE size hints and CLOSE are ignored (CLOSE
only appears where a do-once loop's break leaves a scope with captured locals; leaving that scope
closes them anyway). Two functions are equivalent when a simultaneous walk from their entries
pairs every node with an identically labelled node, consistently.
Usage: python3 -I equiv.py a.luac b.luac [index ...]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from luac51 import OP, decode

KBX = {OP['LOADK'], OP['GETGLOBAL'], OP['SETGLOBAL']}
RKB = {OP['SETTABLE'], OP['EQ'], OP['LT'], OP['LE']} | set(range(OP['ADD'], OP['POW'] + 1))
RKC = {OP['GETTABLE'], OP['SETTABLE'], OP['SELF'], OP['EQ'], OP['LT'], OP['LE']} | set(range(OP['ADD'], OP['POW'] + 1))
COMPARE = {OP['EQ'], OP['LT'], OP['LE']}


class Graph:
    def __init__(self, p):
        self.p = p
        self.ins = [decode(w) for w in p.code]
        self.n = len(self.ins)

    def k(self, x):
        return ('k', self.p.consts[x - 256]) if x >= 256 else x

    def resolve(self, pc):
        """First real node reached from pc (through JMP and CLOSE); 'cycle' for a jump loop."""
        seen = set()
        while pc < self.n and self.ins[pc][0] in (OP['JMP'], OP['CLOSE']):
            if pc in seen:
                return 'cycle'
            seen.add(pc)
            op, sbx = self.ins[pc][0], self.ins[pc][5]
            pc = pc + 1 + sbx if op == OP['JMP'] else pc + 1
        return pc

    def node(self, pc):
        """(label, [successor pcs]) of the instruction at pc."""
        op, a, b, c, bx, sbx = self.ins[pc]
        p = self.p
        if op in KBX:
            return (op, a, ('k', p.consts[bx])), [pc + 1]
        if op == OP['CLOSURE']:
            nups = p.protos[bx].nups
            caps = tuple(self.ins[pc + 1 + i][:3] for i in range(nups))
            return (op, a, bx, caps), [pc + 1 + nups]
        if op == OP['TEST']:
            t = pc + 2 + self.ins[pc + 1][5]
            return (op, a), ([t, pc + 2] if c else [pc + 2, t])
        if op == OP['TESTSET']:
            t = pc + 2 + self.ins[pc + 1][5]
            return (op, a, b, c), [t, pc + 2]
        if op in COMPARE:
            t = pc + 2 + self.ins[pc + 1][5]
            return (op, self.k(b), self.k(c)), ([t, pc + 2] if a else [pc + 2, t])
        if op == OP['LOADBOOL']:
            return (op, a, b), [pc + 2 if c else pc + 1]
        if op == OP['FORPREP']:
            return (op, a), [pc + 1 + sbx]
        if op == OP['FORLOOP']:
            return (op, a), [pc + 1 + sbx, pc + 1]
        if op == OP['TFORLOOP']:
            return (op, a, c), [pc + 2 + self.ins[pc + 1][5], pc + 2]
        if op == OP['RETURN']:
            return (op, a, b), []
        if op == OP['NEWTABLE']:
            return (op, a), [pc + 1]
        if op == OP['SETLIST']:
            return (op, a, b, c if c else self.ins[pc + 1]), [pc + 2 if c == 0 else pc + 1]
        bb = self.k(b) if op in RKB else b
        cc = self.k(c) if op in RKC else c
        return (op, a, bb, cc), [pc + 1]


def equivalent(p1, p2):
    """None when equivalent, else a description of the first mismatch."""
    g1, g2 = Graph(p1), Graph(p2)
    if (p1.numparams, p1.vararg) != (p2.numparams, p2.vararg):
        return 'signature'
    m12, m21 = {}, {}
    stack = [(g1.resolve(0), g2.resolve(0))]
    while stack:
        a, b = stack.pop()
        if a in m12 or b in m21:
            if m12.get(a) != b or m21.get(b) != a:
                return f'merge mismatch at {a} / {b}'
            continue
        m12[a], m21[b] = b, a
        if a == 'cycle' or b == 'cycle':
            if a != b:
                return f'cycle mismatch at {a} / {b}'
            continue
        if a >= g1.n or b >= g2.n:
            if (a >= g1.n) != (b >= g2.n):
                return f'end mismatch at {a} / {b}'
            continue
        (l1, s1), (l2, s2) = g1.node(a), g2.node(b)
        if l1 != l2 or len(s1) != len(s2):
            return f'node {a} {luac51.OPNAMES[l1[0]]} vs {b} {luac51.OPNAMES[l2[0]]}'
        for x, y in zip(s1, s2):
            stack.append((g1.resolve(x), g2.resolve(y)))
    return None


def main(a, b, *idx):
    A = list(luac51.walk(luac51.read_chunk(open(a, 'rb').read())))
    B = list(luac51.walk(luac51.read_chunk(open(b, 'rb').read())))
    sel = [int(i) for i in idx] or range(len(A))
    bad = []
    for i in sel:
        r = equivalent(A[i], B[i])
        if r:
            bad.append(i)
            print(i, r)
    print(f'checked {len(sel)} functions; not equivalent: {len(bad)}')


if __name__ == '__main__':
    main(*sys.argv[1:])
