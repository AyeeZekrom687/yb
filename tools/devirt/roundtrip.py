"""Round-trip check: stripped chunk + inferred locvars -> unluac -> luac5.1 -> compare code.
Usage: python3 -I roundtrip.py in.luac workdir [mine|finder]
Writes workdir/inferred.luac, workdir/decompiled.lua, workdir/recompiled.luac and prints
how many functions recompile to identical instruction streams."""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
from locinfer import infer
from unthread import unthread
import srcfix

# unluac_2025_12_23.jar and the compiled drivers (javac -cp <jar> -d <dir>/driver driver/*.java)
UNLUAC = os.environ.get('UNLUAC_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'unluac'))
CP = UNLUAC + '/unluac_2025_12_23.jar:' + UNLUAC + '/driver'


def strip(p):
    for q in luac51.walk(p):
        q.lineinfo, q.locvars, q.upvalnames = [], [], []
        q.source = None
    p.source = b'=payload'


def main(src, work, mode='mine'):
    os.makedirs(work, exist_ok=True)
    orig = luac51.read_chunk(open(src, 'rb').read())
    strip(orig)
    reference = [list(q.code) for q in luac51.walk(orig)]   # luac re-threads on recompile
    if mode == 'mine':
        print('unthreaded jumps:', unthread(orig))
        infer(orig)
    inferred = os.path.join(work, 'inferred.luac')
    open(inferred, 'wb').write(luac51.write_chunk(orig))
    dec = os.path.join(work, 'decompiled.lua')
    r = subprocess.run(['java', '-Xss64m', '-cp', CP, 'Run', inferred, dec,
                        'nogoto' if mode == 'mine' else 'finder'], stderr=subprocess.PIPE)
    err = r.stderr.decode(errors='replace')
    err = '\n'.join(l for l in err.splitlines() if 'JAVA_TOOL_OPTIONS' not in l)
    if r.returncode != 0 or err.strip():
        print('unluac:', r.returncode, err[:2000])
    # Lua 5.1 has no goto: turn goto/labels into marker calls so only those functions differ
    text = open(dec, encoding='latin-1').read()
    text = srcfix.high_byte_names(text)
    text = re.sub(r'\bgoto (\w+)', r'__goto("\1")', text)
    text = re.sub(r'::(\w+)::', r'__label("\1")', text)
    dec51 = os.path.join(work, 'decompiled51.lua')
    open(dec51, 'w', encoding='latin-1').write(text)
    rec = os.path.join(work, 'recompiled.luac')
    r = subprocess.run(['luac5.1', '-s', '-o', rec, dec51], capture_output=True, text=True, errors='replace')
    if r.returncode != 0:
        print('luac failed:', r.stderr[:2000])
        return
    back = luac51.read_chunk(open(rec, 'rb').read())
    a = list(luac51.walk(orig))
    b = list(luac51.walk(back))
    print(f'functions: original {len(a)}, recompiled {len(b)}')
    same = 0
    first_bad = []
    for i, (x, y) in enumerate(zip(a, b)):
        if reference[i] == y.code and x.consts == y.consts:
            same += 1
        elif len(first_bad) < 15:
            first_bad.append(i)
    print(f'identical code: {same}/{len(a)}')
    print('first differing function indexes:', first_bad)
    text = open(dec, encoding='latin-1').read()
    print('gotos in output:', text.count('goto '), ' lines:', text.count('\n'))


if __name__ == '__main__':
    main(*sys.argv[1:])
