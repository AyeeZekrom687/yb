"""Lift the decoded VM prototypes (protos.jsonl from dump_inject.lua) to a Lua 5.1 binary chunk.

The VM is a re-encoded Lua 5.1 machine: every VM opcode used by the payload maps to one Lua 5.1
opcode with the same operand meanings (A, B, C register/RK fields, Bx constant index, sBx jump
offset biased by 2^23 instead of 131071). Only the encoding differs, so the lift is 1:1.
Usage: python3 -I lift.py protos.jsonl out.luac
"""
import json
import struct
import sys

# VM opcode -> (Lua 5.1 opcode, format)
OPMAP = {
    54: (0, 'ABC'),    # MOVE
    52: (1, 'ABx'),    # LOADK
    31: (2, 'ABC'),    # LOADBOOL
    48: (3, 'ABC'),    # LOADNIL
    46: (4, 'ABC'),    # GETUPVAL
    50: (5, 'ABx'),    # GETGLOBAL
    14: (6, 'ABC'),    # GETTABLE
    61: (7, 'ABx'),    # SETGLOBAL
    69: (8, 'ABC'),    # SETUPVAL
    28: (9, 'ABC'),    # SETTABLE
    32: (10, 'ABC'),   # NEWTABLE
    15: (11, 'ABC'),   # SELF
    43: (12, 'ABC'),   # ADD
    26: (13, 'ABC'),   # SUB
    6: (14, 'ABC'),    # MUL
    45: (15, 'ABC'),   # DIV
    5: (16, 'ABC'),    # MOD
    23: (17, 'ABC'),   # POW
    55: (18, 'ABC'),   # UNM
    33: (19, 'ABC'),   # NOT
    57: (20, 'ABC'),   # LEN
    13: (21, 'ABC'),   # CONCAT
    20: (22, 'AsBx'),  # JMP
    56: (23, 'ABC'),   # EQ
    62: (24, 'ABC'),   # LT
    63: (25, 'ABC'),   # LE
    60: (26, 'ABC'),   # TEST
    64: (27, 'ABC'),   # TESTSET
    51: (28, 'ABC'),   # CALL
    41: (29, 'ABC'),   # TAILCALL
    8: (30, 'ABC'),    # RETURN
    36: (31, 'AsBx'),  # FORLOOP
    10: (32, 'AsBx'),  # FORPREP
    58: (33, 'ABC'),   # TFORLOOP
    19: (34, 'ABC'),   # SETLIST
    3: (35, 'ABC'),    # CLOSE
    67: (36, 'ABx'),   # CLOSURE
    42: (37, 'ABC'),   # VARARG
}
VM_SBX_BIAS = 8388608
LUA_SBX_BIAS = 131071
OP_SETLIST, OP_CLOSURE = 34, 36

# register operands per Lua opcode: which of B/C are registers (RK fields count when < 256)
REG_B = {0, 3, 6, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 27}
REG_C = {6, 9, 11, 12, 13, 14, 15, 16, 17, 21, 23, 24, 25}
RK_B = {9, 12, 13, 14, 15, 16, 17, 23, 24, 25}
RK_C = {6, 9, 11, 12, 13, 14, 15, 16, 17, 23, 24, 25}


def max_register(code):
    """Highest register index an instruction list touches (for maxstacksize)."""
    top = 0
    skip_next = False
    for i, (vop, a, b, c, bx) in enumerate(code):
        if skip_next:
            skip_next = False
            continue
        op = OPMAP[vop][0]
        top = max(top, a)
        if op in REG_B and (op not in RK_B or b < 256):
            top = max(top, b)
        if op in REG_C and (op not in RK_C or c < 256):
            top = max(top, c)
        if op == 11:                      # SELF writes A+1
            top = max(top, a + 1)
        elif op in (28, 29):              # CALL/TAILCALL args and results
            if b > 0:
                top = max(top, a + b - 1)
            if c > 1:
                top = max(top, a + c - 2)
        elif op == 30 and b > 1:          # RETURN
            top = max(top, a + b - 2)
        elif op in (31, 32):              # FORLOOP/FORPREP use A..A+3
            top = max(top, a + 3)
        elif op == 33:                    # TFORLOOP results A+3..A+2+C
            top = max(top, a + 2 + max(c, 1))
        elif op == 37 and b > 1:          # VARARG
            top = max(top, a + b - 2)
        elif op == 34:                    # SETLIST
            top = max(top, a + b)
            if c == 0:
                skip_next = True
        elif op == 3:                     # LOADNIL A..B
            top = max(top, b)
    return top


class Writer:
    def __init__(self):
        self.out = bytearray()

    def byte(self, v):
        self.out.append(v & 0xFF)

    def int(self, v):
        self.out += struct.pack('<i', v)

    def size_t(self, v):
        self.out += struct.pack('<Q', v)

    def instr(self, v):
        self.out += struct.pack('<I', v & 0xFFFFFFFF)

    def number(self, v):
        self.out += struct.pack('<d', v)

    def string(self, b):
        if b is None:
            self.size_t(0)
            return
        self.size_t(len(b) + 1)
        self.out += b + b'\x00'


def encode(vop, a, b, c, bx):
    op, fmt = OPMAP[vop]
    if fmt == 'ABC':
        assert b < 512 and c < 512 and a < 256, (vop, a, b, c)
        return op | (a << 6) | (c << 14) | (b << 23)
    if fmt == 'ABx':
        assert bx < 262144 and a < 256, (vop, a, bx)
        return op | (a << 6) | (bx << 14)
    sbx = bx - VM_SBX_BIAS
    assert -LUA_SBX_BIAS <= sbx <= LUA_SBX_BIAS, (vop, sbx)
    return op | (a << 6) | ((sbx + LUA_SBX_BIAS) << 14)


def dump_function(w, protos, children, pid, source):
    p = protos[pid]
    code = p['code']
    w.string(source)
    w.int(0)                              # linedefined
    w.int(0)                              # lastlinedefined
    w.byte(p['nups'])
    w.byte(p['numparams'])
    w.byte(p['vararg'])
    maxstack = max(max_register(code) + 3, p['numparams'] + 2, 2)
    assert maxstack <= 250, (pid, maxstack)
    w.byte(maxstack)
    w.int(len(code))
    raw_next = False
    for vop, a, b, c, bx in code:
        if raw_next:                      # SETLIST with C == 0: the next word is the real C
            w.instr(bx)
            raw_next = False
            continue
        w.instr(encode(vop, a, b, c, bx))
        if OPMAP[vop][0] == OP_SETLIST and c == 0:
            raw_next = True
    consts = p['consts']
    w.int(len(consts))
    for k in consts:
        t = k['t']
        if t == 'nil':
            w.byte(0)
        elif t == 'bool':
            w.byte(1)
            w.byte(1 if k['v'] else 0)
        elif t == 'num':
            w.byte(3)
            w.number(float(k['v']))
        elif t == 'str':
            w.byte(4)
            w.string(bytes(k['b']))
        else:
            raise ValueError(f'constant type {t} in proto {pid}')
    subs = children.get(pid, [])
    w.int(len(subs))
    for sid in subs:
        dump_function(w, protos, children, sid, None)
    w.int(0)                              # lineinfo
    w.int(0)                              # locvars
    w.int(0)                              # upvalue names


def main(src, dst):
    protos = {}
    children = {}
    for line in open(src):
        p = json.loads(line)
        protos[p['id']] = p
        if p['parent']:
            children.setdefault(p['parent'], []).append((p['index'], p['id']))
    for k in children:
        children[k] = [i for _, i in sorted(children[k])]
    for pid, p in protos.items():
        assert p['nsubs'] == len(children.get(pid, [])), pid
    w = Writer()
    w.out += b'\x1bLua\x51\x00\x01\x04\x08\x04\x08\x00'
    dump_function(w, protos, children, 1, b'=payload')
    open(dst, 'wb').write(bytes(w.out))
    print(f'wrote {len(w.out)} bytes, {len(protos)} functions')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
