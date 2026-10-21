"""List one function's instructions with absolute jump targets.
Usage: python3 -I lst.py chunk.luac index [from to]"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from luac51 import OP

p = list(luac51.walk(luac51.read_chunk(open(sys.argv[1], 'rb').read())))[int(sys.argv[2])]
lo = int(sys.argv[3]) if len(sys.argv) > 3 else 0
hi = int(sys.argv[4]) if len(sys.argv) > 4 else len(p.code)
for pc in range(lo, min(hi, len(p.code))):
    op, a, b, c, bx, sbx = luac51.decode(p.code[pc])
    nm = luac51.OPNAMES[op]
    if op in (OP['JMP'], OP['FORLOOP'], OP['FORPREP']):
        s = f'{a} -> {pc + 1 + sbx}'
    elif op in (OP['LOADK'], OP['GETGLOBAL'], OP['SETGLOBAL']):
        s = f'{a} {p.consts[bx]!r}'
    elif op == OP['CLOSURE']:
        s = f'{a} fn{bx}'
    else:
        k = lambda x: repr(p.consts[x - 256]) if x >= 256 else str(x)
        s = f'{a} {k(b)} {k(c)}'
    print(f'{pc:4d} {nm:<9s} {s}')
