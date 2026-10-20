# YBA Deobfuscation Handoff

## Project Summary
Deobfuscating a Roblox YBA (Your Bizarre Adventure) exploit script. A Python deobfuscator (`fix_deob.py`) applies 12 passes of fixes to undo decompiler bugs on the original decompiled output, producing a working Lua script pushed to GitHub for loading via Roblox executor.

## Repository
- GitHub: `AyeeZekrom687/yb` (branch: `main`, file: `scr`)
- Load URL: `loadstring(game:HttpGet("https://raw.githubusercontent.com/AyeeZekrom687/yb/main/scr?t=" .. tostring(tick())))()`

## Files (all in the repo root)
- `yba_hub_deobfuscated.lua` — Original decompiler output (READ-ONLY reference, latin-1 encoding, 67,383 lines)
- `fix_deob.py` — 12-pass deobfuscator script
- `coordinator_block.lua` — Farm Coordinator module (303 lines, has U+2550 chars that MUST be replaced with `=` for latin-1 compat)
- `scr` — Final output file (deob output + coordinator appended), loaded by Roblox

## Build Process (MUST follow every time)
```bash
# 1. Run deobfuscator
python3 fix_deob.py yba_hub_deobfuscated.lua yba_fixed.lua

# 2. Verify syntax
luac -p yba_fixed.lua

# 3. Insert coordinator block (Python script handles latin-1 + box-drawing replacement)
python3 -I -c "
import re
with open('yba_fixed.lua', 'r', encoding='latin-1') as f:
    content = f.read()
with open('coordinator_block.lua', 'r', encoding='utf-8') as f:
    coord = f.read()
coord = coord.replace('═', '=')
lines = content.split('\n')
insert_idx = None
for i in range(len(lines) - 1, -1, -1):
    if '_r160(_r161)' in lines[i]:
        insert_idx = i + 1
        break
lines.insert(insert_idx, '')
lines.insert(insert_idx + 1, coord)
result = '\n'.join(lines)
with open('scr', 'w', encoding='latin-1') as f:
    f.write(result)
print(f'Written ({result.count(chr(10)) + 1} lines)')
"

# 4. Final verify
luac -p scr
grep -cP '\xEF\xBF\xBD' scr   # MUST be 0

# 5. Push
git add scr && git commit -m "description" && git push -u origin main
```

## Decompiler Bug Rules
1. **ALL bare truthiness tests are INVERTED.** `if _r5 then` in decompiler = `if not _r5 then` in reality. Comparisons (`==`, `~=`, `<`, `>`, `<=`, `>=`) are NOT inverted.
2. **Register reuse**: `_r3`, `_r5` etc. get overwritten with different values later. Closures capture by reference, see stale values.
3. **Dropped assignments outside if blocks**: `if not VAR then end; VAR = VALUE` should be `if not VAR then; VAR = VALUE; end`
4. **Dropped return-false blocks**: `VAR = false; do return VAR end` placed unconditionally outside their if/elseif blocks
5. **Missing breaks** in `while true do` loops
6. **Latin-1 encoding**: File has non-UTF-8 bytes. MUST read/write as latin-1, never UTF-8.

## The 12 Passes in fix_deob.py
| Pass | What | Count |
|------|------|-------|
| 1 | Truthiness inversions | 5,784 |
| 2 | Setlist bugs | 198 fixed, 66 skipped |
| 3 | Targeted patches: namecall varargs, game-ready break, standfarm bridge | 6 |
| 4 | Broken if/else reconstruction (with call-next-line guard) | 493 |
| 5 | Varargs passthrough wrappers | 98 |
| 6 | Timeout breaks | 5 |
| 7 | Branding replacements | 39 |
| 8 | Key system stubs | 1 |
| 9 | Missing breaks in while-true loops + safety yields | 10 + 101 |
| 10 | Players service upvalue fixes (all _rN = _rM.LocalPlayer) | 75 |
| 11 | Dropped return-false blocks | 26 |
| 12 | Game-ready initial skip (close if-not before outer while) | 1 |

## Current Status
- Script PARTIALLY works in Roblox: character "bugs out" (hooks running), but **no UI appears** and no console errors
- Pass 12 fixed the "attempt to call a boolean value" crash at line 7171
- The game-ready check (`_r65()`) now correctly returns true and the init code path is entered
- MacLib download + loadstring should execute, but UI still doesn't appear

## Key Code Sections (line numbers in `scr` / yba_fixed_base.lua)
- **_r65** (~line 3030): Game-ready check function. Has 3 return-false patterns fixed by Pass 11 (HUD check, character parent, health).
- **_r69** (~line 5180): Initialization trigger function
- **Game-ready flow** (~line 7164-7200): `if _r65 then` block with Pass 12 fix
- **MacLib loading** (~line 7200-7330): Downloads MacLib from 3 CDN URLs, applies gsub patches, runs loadstring
- **Settings/getgenv** (~line 7328-7405): Initializes config values
- **Window creation** (~line 28380): `getgenv().YBAHubWindow = _r108` / `_G.YBAHubWindow = _r108`
- **Coordinator block** (~line 68177): Farm Coordinator module appended at end

## Key Register Mappings (top-level scope)
- `_r3`: Starts as `wait`, then `game`, then `game.GetService`, then `game:GetService("Players")` — gets overwritten later (fixed by Pass 10)
- `_r5`: Starts as "Players" string, reassigned to LocalPlayer
- `_r20`: Logging/warn function (defined at line 127)
- `_r31`: Settings getter function (may be nil if settings system stripped)
- `_r48`: Get-player utility (replaced by Pass 10)
- `_r49`: Get-PlayerGui utility
- `_r65`: Game-ready check function
- `_r69`: Called at line 5180, triggers initialization
- `_r99`: MacLib library object (after line 7324)
- `_r108`: Window object (around line 28380)

## Bugs Fixed So Far
1. LocalPlayer nil error (Pass 10 — all cross-register patterns)
2. "attempt to call a string value" at line 7354 (Pass 4 guard for call-next-line)
3. Script silently hangs (Pass 11 — 3 broken return-false patterns in _r65)
4. "attempt to call a boolean value" at line 7171 (Pass 12 — game-ready initial skip)

## What Needs Investigation
- **Why no UI?** The init code path should now be reached. Possible causes:
  - MacLib download silently fails (inside pcall)
  - loadstring fails silently
  - A remaining inversion bug somewhere between MacLib load and window creation (~7300-28380)
  - A remaining dropped-assignment bug (Pass 4 pattern) in the UI creation path
  - An aborted Sonnet sweep session found (but did NOT push): a MacLib re-download loop, 6 guard patterns, and an anti-teleport else block — these findings should be verified independently

## User Directives
- Do NOT make your own additions — just port the script as-is with bug fixes only
- Client-to-Roblox-server communication (FireServer, InvokeServer, RemoteEvents) must NOT be removed — only the exploit hub's own backend (HWID, key verification, heartbeats) is stubbed
- Farm Coordinator IS explicitly requested
- Keep agent usage minimal and be efficient with token intake
- Roblox uses Luau (not standard Lua 5.1)
- The script runs in a Roblox exploit environment: getgenv(), hookmetamethod, newcclosure, checkcaller, getcallingscript, getnamecallmethod, fireproximityprompt, firesignal
