"""Read and write Lua 5.1 binary chunks (little-endian, 4-byte int, 8-byte size_t, double numbers)."""
import struct

HEADER = b'\x1bLua\x51\x00\x01\x04\x08\x04\x08\x00'

OPNAMES = ('MOVE LOADK LOADBOOL LOADNIL GETUPVAL GETGLOBAL GETTABLE SETGLOBAL SETUPVAL SETTABLE '
           'NEWTABLE SELF ADD SUB MUL DIV MOD POW UNM NOT LEN CONCAT JMP EQ LT LE TEST TESTSET '
           'CALL TAILCALL RETURN FORLOOP FORPREP TFORLOOP SETLIST CLOSE CLOSURE VARARG').split()
OP = {name: i for i, name in enumerate(OPNAMES)}


class Proto:
    """One function prototype. code holds raw 32-bit instruction words."""

    def __init__(self):
        self.source = None
        self.linedefined = 0
        self.lastlinedefined = 0
        self.nups = 0
        self.numparams = 0
        self.vararg = 0
        self.maxstack = 2
        self.code = []
        self.consts = []          # None | bool | float | bytes
        self.protos = []
        self.lineinfo = []
        self.locvars = []         # (name bytes, startpc, endpc)
        self.upvalnames = []


def decode(word):
    """Split an instruction word into (op, a, b, c, bx, sbx)."""
    op = word & 0x3F
    a = (word >> 6) & 0xFF
    c = (word >> 14) & 0x1FF
    b = (word >> 23) & 0x1FF
    bx = word >> 14
    return op, a, b, c, bx, bx - 131071


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, fmt):
        v = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return v[0]

    def string(self):
        n = self.take('<Q')
        if n == 0:
            return None
        s = self.data[self.pos:self.pos + n - 1]
        self.pos += n
        return s


def _read_function(r):
    p = Proto()
    p.source = r.string()
    p.linedefined = r.take('<i')
    p.lastlinedefined = r.take('<i')
    p.nups = r.take('<B')
    p.numparams = r.take('<B')
    p.vararg = r.take('<B')
    p.maxstack = r.take('<B')
    p.code = [r.take('<I') for _ in range(r.take('<i'))]
    for _ in range(r.take('<i')):
        t = r.take('<B')
        if t == 0:
            p.consts.append(None)
        elif t == 1:
            p.consts.append(bool(r.take('<B')))
        elif t == 3:
            p.consts.append(r.take('<d'))
        elif t == 4:
            p.consts.append(r.string())
        else:
            raise ValueError(f'bad constant tag {t}')
    p.protos = [_read_function(r) for _ in range(r.take('<i'))]
    p.lineinfo = [r.take('<i') for _ in range(r.take('<i'))]
    for _ in range(r.take('<i')):
        name = r.string()
        p.locvars.append((name, r.take('<i'), r.take('<i')))
    p.upvalnames = [r.string() for _ in range(r.take('<i'))]
    return p


def read_chunk(data):
    """Parse a whole chunk and return the main Proto."""
    if data[:12] != HEADER:
        raise ValueError('not a little-endian 64-bit Lua 5.1 chunk')
    r = _Reader(data)
    r.pos = 12
    return _read_function(r)


class _Writer:
    def __init__(self):
        self.out = bytearray(HEADER)

    def put(self, fmt, v):
        self.out += struct.pack(fmt, v)

    def string(self, s):
        if s is None:
            self.put('<Q', 0)
            return
        self.put('<Q', len(s) + 1)
        self.out += s + b'\x00'


def _write_function(w, p):
    w.string(p.source)
    w.put('<i', p.linedefined)
    w.put('<i', p.lastlinedefined)
    for v in (p.nups, p.numparams, p.vararg, p.maxstack):
        w.put('<B', v)
    w.put('<i', len(p.code))
    for word in p.code:
        w.put('<I', word & 0xFFFFFFFF)
    w.put('<i', len(p.consts))
    for k in p.consts:
        if k is None:
            w.put('<B', 0)
        elif isinstance(k, bool):
            w.put('<B', 1)
            w.put('<B', 1 if k else 0)
        elif isinstance(k, float):
            w.put('<B', 3)
            w.put('<d', k)
        else:
            w.put('<B', 4)
            w.string(k)
    w.put('<i', len(p.protos))
    for sub in p.protos:
        _write_function(w, sub)
    w.put('<i', len(p.lineinfo))
    for line in p.lineinfo:
        w.put('<i', line)
    w.put('<i', len(p.locvars))
    for name, start, end in p.locvars:
        w.string(name)
        w.put('<i', start)
        w.put('<i', end)
    w.put('<i', len(p.upvalnames))
    for name in p.upvalnames:
        w.string(name)


def write_chunk(main):
    w = _Writer()
    _write_function(w, main)
    return bytes(w.out)


def walk(p):
    """Yield every Proto in depth-first order (main first)."""
    yield p
    for sub in p.protos:
        yield from walk(sub)
