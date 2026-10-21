"""List functions whose recompiled code differs from the reference, ignoring NEWTABLE size hints.
Usage: python3 -I diffsum.py ref.luac rec.luac [final.luac]
With final.luac, a function that matches the transformed bytecode is marked 'transformed'."""
import sys, difflib, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from verify import norm
from luac51 import OP


def n2(p):
    return [(x[0], x[1], 0, 0) if x[0] == OP['NEWTABLE'] else x for x in norm(p)]


def load(path):
    return list(luac51.walk(luac51.read_chunk(open(path, 'rb').read())))


A, B = load(sys.argv[1]), load(sys.argv[2])
F = load(sys.argv[3]) if len(sys.argv) > 3 else None
for i, (a, b) in enumerate(zip(A, B)):
    x, y = n2(a), n2(b)
    if x == y:
        continue
    if F and n2(F[i]) == y:
        print(i, 'transformed')
        continue
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
    ops = [(t, i2 - i1, j2 - j1) for t, i1, i2, j1, j2 in sm.get_opcodes() if t != 'equal']
    print(i, len(a.code), len(b.code), ops[:6])
