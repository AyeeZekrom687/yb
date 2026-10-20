"""Build scr: console logger on top, deobfuscated script, Farm Coordinator after the _r160(_r161) call.

usage: python3 -I tools/build.py yba_fixed.lua coordinator_block.lua console_logger.lua scr
"""
import sys

src, coordf, loggerf, out = sys.argv[1:5]
with open(src, 'r', encoding='latin-1') as f:
    content = f.read()
with open(coordf, 'r', encoding='utf-8') as f:
    coord = f.read().replace('═', '=')
with open(loggerf, 'r', encoding='ascii') as f:
    logger = f.read().rstrip('\n')
lines = content.split('\n')
insert_idx = next(i for i in range(len(lines) - 1, -1, -1) if '_r160(_r161)' in lines[i]) + 1
lines[insert_idx:insert_idx] = ['', coord]
result = logger + '\n\n' + '\n'.join(lines)
with open(out, 'w', encoding='latin-1') as f:
    f.write(result)
print(f'Written ({result.count(chr(10)) + 1} lines), coordinator at {insert_idx}')
