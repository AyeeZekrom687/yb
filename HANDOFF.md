# YBA Deobfuscation Handoff

## Project Summary
Deobfuscating a Roblox YBA (Your Bizarre Adventure) exploit script. A Python deobfuscator (`fix_deob.py`) applies a series of passes that undo decompiler bugs in the original decompiled output, producing a Lua script pushed to GitHub for loading via a Roblox executor.

## Repository
- GitHub: `AyeeZekrom687/yb` (branch: `main`, file: `scr`)
- Load URL: `loadstring(game:HttpGet("https://raw.githubusercontent.com/AyeeZekrom687/yb/main/scr?t=" .. tostring(tick())))()`

## Files
- `yba_hub_deobfuscated.lua` - Original decompiler output (READ-ONLY reference, latin-1 encoding, 67,383 lines)
- `fix_deob.py` - Deobfuscator (passes listed below; each pass has a comment block explaining the bug it fixes)
- `coordinator_block.lua` - Farm Coordinator module (U+2550 box characters are replaced with `=` by the build)
- `console_logger.lua` - Mirrors the client console (LogService.MessageOut + history) to `YBA/console_log.txt`; prepended to `scr` by the build (user request)
- `tools/build.py` - Assembles `scr`: logger on top, deobfuscated script, coordinator after the `_r160(_r161)` call
- `scr` - Final output file, loaded by Roblox. Its line numbers are `yba_fixed.lua` line + 115 (the logger block)
- `tools/harness.lua` - Lua 5.1 mock Roblox/executor environment for tracing `scr` (see "Testing")

## Build Process (MUST follow every time)
```bash
set -o pipefail
python3 fix_deob.py yba_hub_deobfuscated.lua yba_fixed.lua
luac -p yba_fixed.lua
python3 -I tools/build.py yba_fixed.lua coordinator_block.lua console_logger.lua scr
luac -p scr
grep -cP '\xEF\xBF\xBD' scr   # MUST be 0
luau-compile --null scr        # real Luau compiler (github.com/luau-lang/luau releases)
git add scr fix_deob.py && git commit -m "description" && git push -u origin main
```
Never pipe the build through `tail`/`grep` without `set -o pipefail`: an assertion in a pass would be hidden and an old `yba_fixed.lua` would be used. A full run of `fix_deob.py` takes about 17 s.

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
11. **Latin-1 encoding.** Read/write as latin-1. Split lines on `\n` only (`split_lines`), never `str.splitlines()`: the file contains 0x85 inside strings. Multi-line strings continue at column 0 (previous line ends in `\`): indentation-based scans must skip those lines.
12. **TESTSET needs an operand body.** Rule 2 applies only when the if-body is empty or its first mention of X writes X. A body that reads X first (`x:Destroy()`, `x:IsA(...)`) or never mentions it (`return true`) is a plain MOVE + TEST and is flipped. TESTSET targets can be parameters (`p = tonumber(p) or 0.45`).
13. **Constant ternaries.** `x = c and K or D` with a literal K prints as `if c then x = K; if not x then D end end`: the path where c is false (straight to D) is lost and x stays nil (81 sites: every English UI/webhook string, `ON`/`OFF`, speeds).
14. **Lost gotos.** Jumps the decompiler could not structure print as `-- goto [N]` (N = 1-based instruction index in the function) and fall through. Counting: one instruction per line, `if` = 2, `else` = 1, loop `end` = 1, other `end`/`while true do` = 0, closure = 1. Dropped instructions make the count drift, so only exact matches against structural join points are used.
15. **Double while loops.** `while true do / while true do BODY end / end` (outer body = inner loop) is one loop: as printed, `break` restarts it (the number formatter spun forever below 1000), and the code after the loop is printed at the end of BODY (a trailing return, e.g. `return formatted`), so the loop ran once.
16. **Unnameable registers.** For-loop control registers (`in _rG, _rS, _rC` / `= _rA, _rB, _rC`) read in the loop body, and copies indexed with a member that exists only on one service (`.RenderStepped`, `.GetPlayerFromCharacter`, ...), are the enclosing variable of that name. A decompiler local that is dead where a nested closure is created (a call clobbered it), or holds a boolean/number literal while the copy is indexed, is passed through to the next owner.
17. **Register reuse evidence (13b).** A main-chunk register R captured by a closure is reused when its next write changes the value kind (copies take their source's kind; a function replacing a function counts), when a later call is based at or below R (calls sit at the stack top), when the next write sets up an argument of a call based below R, when the closure indexes R as an object and R later gets a string/number/boolean, or when R later gets a function literal and did not hold one (e.g. `lastTeleportTick` in register 89, later reused for the farm activity check). The split range starts at the latest write in the closure's own block, so conditional writes in between stay consistent.

## Passes in fix_deob.py (current counts)
| Pass | What | Count |
|------|------|-------|
| 0b | Structural repairs (loop end, bypass else, GameModes or-chain, outer game-ready while) | 4 |
| 0d | Lost gotos: `F = true` at the goto, skipped code wrapped in `if not F then`, `if F then break end` where a loop is left | 30 resolved, 141 left |
| 0c | Double while loops collapsed / post-loop code moved out | 49 / 22 |
| 0 | TESTSET restoration (marked / operands moved / ternary else) | 394 / 32 / 11 |
| 1 | Truthiness inversions (TEST) | 5,390 |
| 1b | Boolean materializations | 52 |
| 2 | Setlist bugs | 198 fixed, 66 skipped |
| 2b | Multi-return forwarding | 210 |
| 3 | Targeted patches (namecall varargs, game-ready break, standfarm bridge, first-spawn loop) | 10 |
| 3b | Or-chains | 689 |
| 4 | Dropped right operands and guarded call bodies (register tests and comparisons; `if A then ...; if B then end; BODY else E end` becomes `if A and B then BODY else E end` via an `_andN` flag) | 198 |
| 4b | Constant ternaries (else restored) | 81 |
| 5 | Varargs passthrough wrappers | 98 |
| 6 | Timeout breaks | 5 |
| 7 | Branding replacements | 48 |
| 8 | Key system stubs | 1 |
| 8b | While-condition chains | 106 |
| 8c | Loop tail exits (yields moved / success breaks) | 39 / 32 |
| 9 | Remaining while-true guard breaks + safety yields | 2 + 16 |
| 10 | Players service upvalue fixes | 75 |
| 11 | Dropped return-false | 0 |
| 12 | Game-ready initial skip (structure already correct after 0c/0d) | 0 |
| 13 | Scope leaks (202 renamed for collisions, incl. forced upvalues and dead lexical owners) | 1,694 |
| 13b | Closed upvalues (main-chunk registers split per captured range) | 312 |
| 14 | Garbled empty strings | 220 |

Patch 3f (UI restore loop) was removed: Pass 0c produces the same structure generically.

## Testing (tools/harness.lua)
Runs `scr` under Lua 5.1 with mocked `game`/Instances/services, `task.*` scheduler, executor API, an in-memory filesystem and a fake MacLib. Prints the boot log (`_r20` writes `YBA/boot_log.txt`), every error swallowed by `pcall`, task errors, and infinite loops (instruction budget). Callbacks connected to RunService frame signals are fired every simulated frame, common signals reject members other than Connect/Once/Wait, and only method-like members are callable (calling an Instance errors).
```bash
curl -sS -o maclib.lua https://raw.githubusercontent.com/AZX0OZ/maclib/refs/heads/main/maclib
# in-game scenario (menu / loading GUIs absent):
MACLIB_PATH=maclib.lua ABSENT="LoadingScreen1,LoadingScreen,BlackScreen,Main Menu" lua5.1 tools/harness.lua scr 50000000
# main-menu scenario: same without ABSENT
# switch on toggles / press buttons after boot (case-insensitive name match, "*" = all), 200 x 0.5 s:
FIRE="Enable farming" FIRE_STEPS=200 MACLIB_PATH=... ABSENT=... lua5.1 tools/harness.lua scr 50000000
```
With FIRE set, repeated output lines are counted (log spam shows as `N x line`). Mock limitations: every child exists unless listed in ABSENT, numeric properties (`.X`, `.Value`) return numbers (so `UDim2.X.Scale` errors at scr:30519 and `PlayerStats.Quest.Value` is a number at scr:37049), MacLib is a stub, the world has no items.

## Current Status
- Builds 8d707ab, dbd97d4, 253ad8a: see git log; their reported errors are reproduced and cleared in the harness.
- Round 3 report (253ad8a): watermark `RenderStepped` on a ScreenGui (32513), `GetPlayerFromCharacter` nil (41841/41924), `Destroy` on nil (52419/52503), repeating `number < function` (56130). Causes: service-member collision, for-loop control register read, TESTSET false positive, main register 160 reused as an `ipairs` generator. All fixed (rules 12, 16, 17).
- Item farm toggle ("spams logs and does not work"): the watchdog's `if status == "dead"` lost its body, so every 2 s it warned and spawned another farm thread; the farm loop itself hit a nil multiplier (lost ternary), spun without yielding (double while), and had 7 lost gotos. All fixed; in the harness the farm runs with no errors and a constant task count.
- Round 4 report (113513c): `scr:29544 attempt to call a number value` in the item farm watchdog. Main register 89 held `lastTeleportTick` (written by the teleport marker closure) and was then reused for the farm activity function, so the first teleport overwrote it. Fixed by the function-literal rule above. The same watchdog lost its `and`: the stall recovery (no farm action for 8 s) only ran when no farm thread existed; now `if thread and status == "dead" then resurrect else stall check end`.
- Harness, current build: in-game and main-menu boot complete (8 tabs, 62 toggles); firing all 77 toggles/buttons leaves only the mock artifacts and the AutoPlay log collision below.

## Known Residual Issues
- **141 lost gotos still fall through** (no exact join point). Most are in long if/elseif chains (item-name normalizer: a later match can overwrite the result) and in the server-hop page loop.
- **AutoPlay stage-click log** (scr:5430, main menu only, inside pcall): `_r21 = _r20` copies the main logger while the function's own `_r20` holds the stage from a multi-return call; no literal evidence to separate them.
- `isSellableItem` / `isCandyItem` / `isProtectedItem` / `sellableItems` are read as globals in a few places but only defined as `_r142` fields (pre-existing in the original).
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
