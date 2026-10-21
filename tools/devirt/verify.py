"""Recompile a decompiled source and compare every function with a reference chunk.
Constants are compared by value (compilers number them differently), functions by tree position.
Usage: python3 -I verify.py reference.luac decompiled.lua luac_binary [list]"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from luac51 import OP

KBX = {OP['LOADK'], OP['GETGLOBAL'], OP['SETGLOBAL']}
RKB = {OP['SETTABLE'], OP['EQ'], OP['LT'], OP['LE']} | set(range(OP['ADD'], OP['POW'] + 1))
RKC = {OP['GETTABLE'], OP['SETTABLE'], OP['SELF'], OP['EQ'], OP['LT'], OP['LE']} | set(range(OP['ADD'], OP['POW'] + 1))


def norm(p):
    """Instruction list with constant operands replaced by their values."""
    out = []
    skip = False
    for w in p.code:
        if skip:
            out.append(('raw', w))
            skip = False
            continue
        op, a, b, c, bx, sbx = luac51.decode(w)
        if op in KBX:
            out.append((op, a, ('k', p.consts[bx])))
            continue
        if op == OP['CLOSURE']:
            out.append((op, a))
            continue
        bb = ('k', p.consts[b - 256]) if op in RKB and b >= 256 else b
        cc = ('k', p.consts[c - 256]) if op in RKC and c >= 256 else c
        if op in (OP['JMP'], OP['FORLOOP'], OP['FORPREP']):
            out.append((op, a, sbx))
        else:
            out.append((op, a, bb, cc))
        if op == OP['SETLIST'] and c == 0:
            skip = True
    return out


def compare(x, y, path, res):
    same = norm(x) == norm(y) and x.numparams == y.numparams and x.vararg == y.vararg
    res.append((path, same, len(x.code), len(y.code)))
    if len(x.protos) != len(y.protos):
        res.append((path + ('children %d vs %d' % (len(x.protos), len(y.protos)),), False, 0, 0))
        return
    for i, (cx, cy) in enumerate(zip(x.protos, y.protos)):
        compare(cx, cy, path + (i,), res)


def main(ref, src, luac, show=None):
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, 'rec.luac')
        r = subprocess.run([luac, '-s', '-o', out, src], capture_output=True, text=True,
                           errors='replace')
        if r.returncode != 0:
            print('compile failed:', r.stderr.strip())
            return
        back = luac51.read_chunk(open(out, 'rb').read())
    res = []
    compare(luac51.read_chunk(open(ref, 'rb').read()), back, (), res)
    same = sum(1 for r in res if r[1])
    print(f'compared {len(res)} functions; identical {same}')
    bad = [r for r in res if not r[1]]
    shape = [r for r in bad if r[0] and isinstance(r[0][-1], str)]
    for r in shape:
        print('tree differs at', r[0])
    if show:
        print('differing:', [r[0] for r in bad][:60])


if __name__ == '__main__':
    main(*sys.argv[1:])
