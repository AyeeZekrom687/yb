"""Lifted chunk -> structured, goto-free Lua 5.1 source.

Steps: strip debug info, undo jump threading, make do-once/continue jumps visible as loops
(iterating with unluac's own label report), infer locals, decompile with unluac (no goto),
then source fixes.
Usage: python3 -I pipeline.py lifted.luac workdir
Outputs workdir/final.luac (transformed bytecode with inferred debug info) and
workdir/decompiled.lua.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import luac51
import srcfix
from chains import rewrite_all
from doonce import find_region, insert_loop, jump_into_else
from locinfer import Code, infer
from roundtrip import strip
from unthread import unthread, _pseudo, _uncond

# unluac_2025_12_23.jar and the compiled drivers (javac -cp <jar> -d <dir>/driver driver/*.java)
UNLUAC = os.environ.get('UNLUAC_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'unluac'))
CP = UNLUAC + '/unluac_2025_12_23.jar:' + UNLUAC + '/driver'


def java(*args):
    r = subprocess.run(['java', '-Xss64m', '-cp', CP] + list(args), capture_output=True)
    return r.stdout.decode('latin-1'), r.stderr.decode('latin-1')


def label_targets(path):
    """{function preorder index: [label pcs]} from unluac in goto mode."""
    out, _ = java('LabelDump', path)
    labels = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] != 'ERROR':
            labels[int(parts[0])] = [int(x) for x in parts[1:]]
    return labels


def is_break_skip(proto, t):
    """`if cond then break end`: the condition's jumps skip one forward break JMP right before t;
    unluac renders that without goto in nogoto mode."""
    ins = [luac51.decode(w) for w in proto.code]
    pseudo = _pseudo(proto, ins)
    srcs = [pc for pc, i in enumerate(ins) if not pseudo[pc] and i[0] == luac51.OP['JMP']
            and pc + 1 + i[5] == t]
    if not srcs or not _uncond(ins, pseudo, t - 1) or ins[t - 1][5] < 0:
        return False
    return all(pc < t - 1 and not _uncond(ins, pseudo, pc) for pc in srcs)


def active_counts(proto):
    code = Code(proto)
    decls = code.walk()
    nact = [0] * code.n
    for d in decls:
        for pc in range(d[1], min(d[2], code.n)):
            nact[pc] += 1
    return code, nact


def make_loops(main, work):
    """Iteratively wrap unluac's goto targets in loops. Returns the number of loops inserted."""
    total = 0
    done = set()                  # (function, region start) already wrapped: never wrap again
    for _ in range(6):
        tmp = os.path.join(work, 'iter.luac')
        snap = luac51.read_chunk(luac51.write_chunk(main))
        for a, b in zip(luac51.walk(main), luac51.walk(snap)):
            b.chain_marks = getattr(a, 'chain_marks', [])     # not part of the chunk format
        infer(snap)
        open(tmp, 'wb').write(luac51.write_chunk(snap))
        labels = label_targets(tmp)
        funcs = list(luac51.walk(main))
        for fi, p in enumerate(funcs):
            extra = jump_into_else(p)
            if extra:
                labels[fi] = sorted(set(labels.get(fi, ())) | extra)
        inserted = 0
        for fi, targets in labels.items():
            p = funcs[fi]
            for t in sorted(targets, reverse=True):
                if is_break_skip(p, t):
                    continue
                code, nact = active_counts(p)
                s = find_region(code.ins, code.pseudo, code.live, nact, t)
                if s is None:
                    print(f'  function {fi}: no region for label at pc {t}')
                    continue
                if (fi, s) in done:
                    print(f'  function {fi}: label at pc {t} persists after wrapping at {s}; skipped')
                    continue
                done.add((fi, s))
                print(f'  function {fi}: loop [{s}, {t})')
                insert_loop(p, s, t)
                inserted += 1
        total += inserted
        if not inserted:
            break
    return total


def main(src, work):
    os.makedirs(work, exist_ok=True)
    m = luac51.read_chunk(open(src, 'rb').read())
    strip(m)
    print('unthreaded jumps:', unthread(m))
    print('mixed and/or chains rewritten:', rewrite_all(m))
    print('loops inserted:', make_loops(m, work))
    infer(m)
    final = os.path.join(work, 'final.luac')
    open(final, 'wb').write(luac51.write_chunk(m))
    dec = os.path.join(work, 'decompiled.lua')
    out, err = java('Run', final, dec, 'nogoto')
    err = '\n'.join(l for l in err.splitlines() if 'JAVA_TOOL_OPTIONS' not in l)
    if err.strip():
        print('unluac:', err[:2000])
    text = open(dec, encoding='latin-1').read()
    text = srcfix.high_byte_names(text)
    open(dec, 'w', encoding='latin-1').write(text)
    r = subprocess.run(['luac5.1', '-p', dec], capture_output=True, text=True, errors='replace')
    print('luac -p:', 'ok' if r.returncode == 0 else r.stderr.strip())
    print('lines:', text.count('\n'), 'gotos:', text.count('goto '))


if __name__ == '__main__':
    main(*sys.argv[1:])
