"""Show an instruction-level diff of one function (preorder index) between two chunks.
Usage: python3 -I fndiff.py ref.luac other.luac index"""
import sys, difflib, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from verify import norm

a = list(luac51.walk(luac51.read_chunk(open(sys.argv[1], 'rb').read())))[int(sys.argv[3])]
b = list(luac51.walk(luac51.read_chunk(open(sys.argv[2], 'rb').read())))[int(sys.argv[3])]


def fmt(t):
    return luac51.OPNAMES[t[0]] + ' ' + ' '.join(repr(x)[:40] for x in t[1:])


na = [fmt(t) for t in norm(a)]
nb = [fmt(t) for t in norm(b)]
for line in difflib.unified_diff(na, nb, 'ref', 'rec', n=4, lineterm=''):
    print(line)
