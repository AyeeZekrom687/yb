"""Undo luac jump threading at loop exits (semantics-preserving bytecode rewrite).

luac's luaK_jump chains every jump pending at the current pc into a JMP emitted there, so a
loop whose exit lands on an unconditional JMP gets its exits retargeted to that JMP's
destination. unluac cannot always rebuild such loops. Retargeting those exits back to the JMP
right after the loop restores the canonical shape; the JMP goes to the same place, so behaviour
is unchanged.
"""
from luac51 import OP, decode

TESTS = {OP['EQ'], OP['LT'], OP['LE'], OP['TEST'], OP['TESTSET']}


def _encode_sbx(word, sbx):
    return (word & 0x3FFF) | ((sbx + 131071) << 14)


def _jumps(ins, pseudo):
    """[(pc, target)] of every JMP (conditional or not)."""
    out = []
    for pc, (op, a, b, c, bx, sbx) in enumerate(ins):
        if not pseudo[pc] and op == OP['JMP']:
            out.append((pc, pc + 1 + sbx))
    return out


def _pseudo(proto, ins):
    pseudo = [False] * len(ins)
    for pc, (op, a, b, c, bx, sbx) in enumerate(ins):
        if pseudo[pc]:
            continue
        if op == OP['CLOSURE']:
            for k in range(proto.protos[bx].nups):
                pseudo[pc + 1 + k] = True
        elif op == OP['SETLIST'] and c == 0:
            pseudo[pc + 1] = True
    return pseudo


def _uncond(ins, pseudo, pc):
    return (0 <= pc < len(ins) and not pseudo[pc] and ins[pc][0] == OP['JMP']
            and (pc == 0 or pseudo[pc - 1] or ins[pc - 1][0] not in TESTS))


def loops(ins, pseudo):
    """[(head, last, exit)] for every loop: body pcs head..last, natural exit pc."""
    out = []
    last_back = {}
    for pc, (op, a, b, c, bx, sbx) in enumerate(ins):
        if pseudo[pc]:
            continue
        if op == OP['JMP'] and sbx < 0:
            head = pc + 1 + sbx
            last_back[head] = max(last_back.get(head, -1), pc)
        elif op == OP['FORPREP']:
            q = pc + 1 + sbx                       # FORLOOP
            out.append((pc + 1, q, q + 1))
        elif op == OP['TFORLOOP']:
            prep = pc + 1 + ins[pc + 1][5]         # prep JMP before the body
            out.append((prep + 1, pc + 1, pc + 2))
    for head, back in last_back.items():
        out.append((head, back, back + 1))
    return out


def unthread_function(proto):
    """Rewrite proto.code in place; returns the number of retargeted jumps."""
    ins = [decode(w) for w in proto.code]
    pseudo = _pseudo(proto, ins)
    changed = 0
    # while loops: a jump inside the body to the loop head was threaded through the back JMP
    # (it ended an if statement, or a continue, at the end of the body); point it at the back
    # JMP, the canonical end-of-body target
    backs = {}
    for pc, target in _jumps(ins, pseudo):
        if target <= pc and _uncond(ins, pseudo, pc):
            backs[target] = max(backs.get(target, -1), pc)
    for head, back in backs.items():
        for pc, target in _jumps(ins, pseudo):
            if head <= pc < back and target == head:
                proto.code[pc] = _encode_sbx(proto.code[pc], back - (pc + 1))
                ins[pc] = decode(proto.code[pc])
                changed += 1
    for head, last, exit_pc in sorted(loops(ins, pseudo), key=lambda l: l[1] - l[0]):
        if not _uncond(ins, pseudo, exit_pc):
            continue
        final = exit_pc + 1 + ins[exit_pc][5]
        if head <= final <= exit_pc:
            continue                               # the JMP after the loop goes back into it
        for pc, target in _jumps(ins, pseudo):
            if head <= pc <= last and target == final:
                proto.code[pc] = _encode_sbx(proto.code[pc], exit_pc - (pc + 1))
                ins[pc] = decode(proto.code[pc])
                changed += 1
    return changed


def unthread(main):
    total = 0
    stack = [main]
    while stack:
        p = stack.pop()
        total += unthread_function(p)
        stack.extend(p.protos)
    return total
