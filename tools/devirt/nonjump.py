"""Compare functions ignoring jumps, NEWTABLE size hints and test polarity (the C/A flag of
TEST/EQ/LT/LE flips when an if is restructured around a do-once loop).
Usage: python3 -I nonjump.py ref.luac rec.luac index ..."""
import sys, os, difflib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from verify import norm
from luac51 import OP

POL_A = {OP['EQ'], OP['LT'], OP['LE']}


def nj(p):
    out = []
    for x in norm(p):
        if x[0] == OP['JMP']:
            continue
        if x[0] == OP['NEWTABLE']:
            x = (x[0], x[1], 0, 0)
        elif x[0] in POL_A:
            x = (x[0], 0) + x[2:]
        elif x[0] == OP['TEST']:
            x = (x[0], x[1], x[2], 0)
        out.append(x)
    return out


A = list(luac51.walk(luac51.read_chunk(open(sys.argv[1], 'rb').read())))
B = list(luac51.walk(luac51.read_chunk(open(sys.argv[2], 'rb').read())))
for i in map(int, sys.argv[3:]):
    x, y = nj(A[i]), nj(B[i])
    if x == y:
        print(i, 'same modulo jumps/polarity')
        continue
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
    for t, i1, i2, j1, j2 in sm.get_opcodes():
        if t != 'equal':
            print(i, t, [luac51.OPNAMES[o[0]] + str(o[1:]) for o in x[i1:i2]][:6], '->',
                  [luac51.OPNAMES[o[0]] + str(o[1:]) for o in y[j1:j2]][:6])
