# YBA Deobfuscation Handoff

## Project Summary
Deobfuscating a Roblox YBA (Your Bizarre Adventure) exploit script. A Python deobfuscator (`fix_deob.py`) applies a series of passes that undo decompiler bugs in the original decompiled output, producing a Lua script pushed to GitHub for loading via a Roblox executor.

## Repository
- GitHub: `AyeeZekrom687/yb` (branch: `main`, file: `scr`)
- Load URL: `loadstring(game:HttpGet("https://raw.githubusercontent.com/AyeeZekrom687/yb/main/scr?t=" .. tostring(tick())))()`

## Files
- `yba_hub_deobfuscated.lua` — Original decompiler output (READ-ONLY reference, latin-1 encoding, 67,383 lines)
- `fix_deob.py` — Deobfuscator (passes listed below; the docstring at the top documents each one)
- `coordinator_block.lua` — Farm Coordinator module (has U+2550 chars that MUST be replaced with `=` for latin-1 compat)
- `scr` — Final output file (deob output + coordinator inserted), loaded by Roblox
- `tools/harness.lua` — Lua 5.1 mock Roblox/executor environment for tracing `scr` (see "Testing")

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
luau-compile --null scr        # optional: real Luau compiler (github.com/luau-lang/luau releases)

# 5. Push
git add scr && git commit -m "description" && git push -u origin main
```
Never pipe the build through `tail`/`grep` without `set -o pipefail`: an assertion in a pass would be hidden and an old `yba_fixed.lua` would be used.

## Decompiler Bug Rules (corrected)
1. **TEST conditions are printed inverted** (`if _r5 then` means `if not _r5 then`). This covers bare tests on a register computed in place and copies from a true upvalue (GETUPVAL + TEST). Comparisons are NOT inverted.
2. **TESTSET conditions are printed correctly.** A copy from the function's OWN register/param directly followed by a test (`_rX = _rY` / `if [not] _rX then`) is `x = y or ...` / `x = y and ...` and must not be flipped (392 sites). Rule 1 alone was wrong for these.
3. **Scope leaks.** `local _rN` is emitted at the first assignment even inside a nested block; uses after the block resolve to a global or to an enclosing function's variable of the same name. Writes then clobber shared state (e.g. `pairs()`'s third value `nil` overwrote the main chunk's UserInputService; AutoPlay overwrote the `_r20` logger with a string).
4. **Register/upvalue name collisions.** Upvalues are named after the parent's register, so a function can use `_r20` both as its own temp and as the main chunk's logger. Upvalues are only reachable through plain copies (`_rX = R` / `R = _rX`); `R = R` is always GETUPVAL into the own register. A closure passed straight to a call (`_rJ(_rJ+1, ..., _rN)`) cannot capture that call's temporaries `_rJ.._r(N-1)`; such names are outer upvalues (e.g. `Heartbeat:Connect(function() ... _r5.Character ...)` where `_r5` is LocalPlayer, not the signal).
A copy `_rX = R` made after a call clobbered the function's own R (R was a call temporary) is also the upvalue, even inside R's `local` scope.
4b. **Register reuse after CLOSE.** The main chunk reuses registers after a block's closures captured them (`_r150 = 30` captured by a Heartbeat callback, later `_r150 = {}` / a function). Pass 13b splits those names per captured range; nil forward declarations that the closure fills in itself count as closure state.
5. **Or-chains.** `if a or b then BODY end` prints as nested tests with an empty innermost `if`, BODY at the end of the outer block or dropped after it. BODY must run iff `(not F1) or (not F2) or ... or C`.
6. **Boolean materializations.** `x = (a == b)` loses `else x = true`, nesting the rest of the block (up to 59k lines) inside the if.
7. **Missing loop exits.** `while A and B do` prints as `while true do` + nested ifs without breaks; search loops lose their success `break` and put the trailing wait in the first test's else; post-loop code lands in the else of the loop test.
8. **Multi-return forwarding.** `f(g())` prints with a fixed range of stale registers as extra arguments (e.g. `tonumber(v, "IESPSize")`).
9. **Setlist packing.** Table constructors pack the final values of registers t+1..L (L = register of the last assignment before the setlist); temporaries are not elements.
10. **Garbled empty strings.** `""` decodes to a different 2-byte garbage constant per function.
11. **Latin-1 encoding.** Read/write as latin-1. Split lines on `\n` only (`split_lines`), never `str.splitlines()`: the file contains 0x85 inside strings.

## Passes in fix_deob.py (current counts)
| Pass | What | Count |
|------|------|-------|
| 0b | Structural repairs (loop end, bypass else, GameModes or-chain, outer game-ready while) | 4 |
| 0 | TESTSET restoration (marked / operands moved / ternary else) | 392 / 32 / 11 |
| 1 | Truthiness inversions (TEST) | 5,392 |
| 1b | Boolean materializations | 53 |
| 2 | Setlist bugs | 198 fixed, 66 skipped |
| 2b | Multi-return forwarding | 210 |
| 3 | Targeted patches (namecall varargs, game-ready break, standfarm bridge, first-spawn loop, UI restore loop) | 8 |
| 3b | Or-chains | 692 |
| 4 | Dropped right operands (`x = x or v`) and guarded call bodies (`if not c then pcall(fn) end`) | 196 |
| 5 | Varargs passthrough wrappers | 98 |
| 6 | Timeout breaks | 5 |
| 7 | Branding replacements | 39 |
| 8 | Key system stubs | 1 |
| 8b | While-condition chains | 97 |
| 8c | Loop tail exits (yields moved / success breaks) | 37 / 32 |
| 9 | Remaining while-true guard breaks + safety yields | 2 + 24 |
| 10 | Players service upvalue fixes | 75 |
| 11 | Dropped return-false (single tests; chains go to 3b) | 0 |
| 12 | Game-ready initial skip | 1 |
| 13 | Scope leaks (registers hoisted; 180 renamed for collisions) | 1,666 |
| 13b | Closed upvalues (main-chunk registers split per captured range) | 230 |
| 14 | Garbled empty strings | 219 |

## Testing (tools/harness.lua)
Runs `scr` under Lua 5.1 with mocked `game`/Instances/services, `task.*` scheduler, executor API, an in-memory filesystem and a fake MacLib. Prints the boot log (`_r20` writes `YBA/boot_log.txt`), every error swallowed by `pcall`, task errors, and infinite loops (instruction budget). Callbacks connected to RunService frame signals (Heartbeat, RenderStepped, ...) are fired every simulated frame, common signals reject members other than Connect/Once/Wait, and only method-like members are callable (calling an Instance errors), so per-frame errors like the F9 spam reproduce.
```bash
curl -sS -o maclib.lua https://raw.githubusercontent.com/AZX0OZ/maclib/refs/heads/main/maclib
# in-game scenario (menu / loading GUIs absent):
MACLIB_PATH=maclib.lua ABSENT="LoadingScreen1,LoadingScreen,BlackScreen,Main Menu" lua5.1 tools/harness.lua scr 50000000
# main-menu scenario: same without ABSENT
```
Expected now: in-game `_r65()` is true on the first call, "FIRST SPAWN" logs once, the window, tab group, 8 tabs, 22 sections, 62 toggles, 16 dropdowns, 9 sliders, 15 buttons are built, and `main chunk returned normally`. Mock limitations: every child exists unless listed in ABSENT, `.X` returns a number (so `UDim2.X.Scale` errors at ~30300), MacLib is a stub.

## Current Status
- Previous blockers (each confirmed in the harness): main chunk crashed at `normalizeItemName` (scope leak), at the settings loader (TESTSET), and looped back into the game-ready wait (outer `while true` + materialization nesting); none occur now.
- Default settings no longer auto-start item farm / stand farm or run Remove Map (or-chain fix).
- First-spawn code no longer re-runs `_r73()` / `_r69()` every frame.
- First Roblox test (build 8d707ab): UI showed for a moment, F9 spammed `Line 18283: arithmetic (mod) on number and function` and `Character is not a valid member of RBXScriptSignal Line 6553`. Causes: the UI visibility task hid the window and its restore never ran (fixed by patch 3f), and the two register bugs above (fixed by the frame-temp rule and Pass 13b). The harness reproduces both errors on 8d707ab and shows none on the current build.
- Second Roblox test (build dbd97d4): UI loaded with no tabs, `Line 31431 attempt to call a Instance value` (`if not gui.Parent then pcall(fn) end` decompiled as an empty if followed by the call; Pass 4 moved only `X = pcall`). The main chunk died before building tabs. Fixed by the guarded-call extension in Pass 4; reproduced and cleared in the harness.

## Known Residual Issues
- **Closure name collisions the liveness rule cannot resolve:** the HUD health loop (`_r159`, closure reads `_r5.PlayerGui` while the parent's own `_r5` is a boolean flag) and AutoPlay's stage-click log (`_r21 = _r20` copies the logger while the own `_r20` holds the stage). Both fail inside `pcall`.
- `isSellableItem` / `isCandyItem` / `isProtectedItem` / `sellableItems` are read as globals in a few places but only defined as `_r142` fields (pre-existing in the original).
- `_r65` indexes the character after the parent check; this matches the decompiled structure.
- 66 setlists are still skipped (nested table constructors).

## Key Code Sections (search by string; line numbers shift between builds)
- `_r65` (`local _r65 = function()`): game-ready check (PlayerGui, LoadingScreen1, BlackScreen, LoadingScreen/GameModes, Main Menu, HUD, character, Humanoid health)
- First-spawn wait: `"FIRST SPAWN (strict main game) reached"`
- MacLib loading: `"downloading maclib..."`
- Window creation: `"UI: window created"`
- Coordinator block: `YBA Farm Coordinator`

## User Directives
- Do NOT make your own additions — just port the script as-is with bug fixes only
- Client-to-Roblox-server communication (FireServer, InvokeServer, RemoteEvents) must NOT be removed — only the exploit hub's own backend (HWID, key verification, heartbeats) is stubbed
- Farm Coordinator IS explicitly requested
- Keep agent usage minimal and be efficient with token intake
- Roblox uses Luau (not standard Lua 5.1)
- The script runs in a Roblox exploit environment: getgenv(), hookmetamethod, newcclosure, checkcaller, getcallingscript, getnamecallmethod, fireproximityprompt, firesignal
