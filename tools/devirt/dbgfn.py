"""Print inferred declarations (with trigger reasons) for payload functions containing all the
given string constants. Usage: python3 -I dbgfn.py chunk.luac const1 [const2 ...] [--code]"""
import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import luac51
from locinfer import Code

args = [a for a in sys.argv[2:] if a != '--code']
m = luac51.read_chunk(open(sys.argv[1], 'rb').read())
for i, p in enumerate(luac51.walk(m)):
    ks = {k for k in p.consts if isinstance(k, bytes)}
    if all(a.encode() in ks for a in args):
        c = Code(p)
        d = c.walk()
        print(f'fn {i} n={c.n} params={p.numparams}')
        for x in d:
            print('  decl', x[:4], 'trigger', x[4])
        if '--code' in sys.argv:
            for pc in range(c.n):
                if c.pseudo[pc]:
                    continue
                op, a, b, cc, bx, sbx = c.ins[pc]
                nm = sum(1 for x in d if x[1] <= pc < x[2])
                k = ''
                if op in (luac51.OP['GETGLOBAL'], luac51.OP['LOADK']):
                    k = p.consts[bx]
                print(f'   {pc:4d} N={nm:<3d} {luac51.OPNAMES[op]:<9s} {a} {b} {cc} sbx={sbx} {k!r} '
                      f'live={[r for r in range(12) if (c.live[pc] >> r) & 1]}')
