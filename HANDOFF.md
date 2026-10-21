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
18. **Copies that cannot be own.** In a call's argument setup, a copy from the callee or an earlier argument register of the same call (`TeleportToPlaceInstance(id, job, job)`, `FireServer("X", "X")`); a copy that is then called while the function's latest own write of the source is a string/number/boolean/table/nil literal (or indexed while it is a number/boolean/nil); the same source copied into both the callee and an argument (`f(f)`). All are the enclosing variable.
19. **Near-miss goto targets.** A count off by 1-2 is accepted only if no register written in the skipped code is read after the target before being rewritten, the target does not assign the register set right before the goto, and the target does not directly follow a return/break (a drifted count there is the return itself).
20. **Or-of-and chains.** `if A or (B and C) or (D and not E) then` prints as nested tests where an empty `if` jumps on its printed condition: to the result when another test follows, to failure when the result follows. Single-register chains with an empty test before the last one are rebuilt by Pass 3a (non-empty `if not R` = alternative, non-empty `if R` = and-term member, empty test closes the and-term); the item-name normalizer by Pass 15. A chain whose register is read afterwards is a value expression (`return a or (b and c) or true`): its trailing statements are another alternative, not a body, and it is left to Pass 3b.
21. **Or-chains with an else.** `if a or b then X else E end` prints as rule 5 with the else on the outermost `if`; the else is the branch taken when the whole chain fails (reference: the JSON settings encoder's `type(k) ~= "number" or k < 1 or k % 1 ~= 0`, which as printed made every numeric key non-array and compared numbers with strings). The same shape had been read as an and-chain (the old Pass 4 `_andN` rule); 20 of the 22 sites only make sense as or-chains, so that rule was removed.
22. **Or-chain operand at an and-chain tail.** `x = (A and B and C) or K` prints with the TESTSET `x = C / if not x then end` as the last statement of the innermost and-level and `x = K` after the chain's closing `end`. It must not be marked as TESTSET (Pass 0); Pass 3b rebuilds it. As printed, `x = K` always ran (money read as 0 by auto-buy, the RemoteFunction lookup always nil).
23. **Recursive local functions.** `local function f` prints as `local f = function`, so the body's `f` is the enclosing scope's (nil, or a different function of the same register: the quest routine's retry called the main chunk's ErrorMessageChanged hook setup). Pass 13e.
24. **Lost repeat-until exits.** `repeat BODY until A or B` prints as `while true do` without any exit whose body ends in the or-chain test, holding the code after the loop (sometimes the whole rest of the function, and the enclosing `if`'s tail). Recognised by a timeout in the chain (a register counted up in the body, or `tick() - start`, compared with a literal). Pass 8d. As printed these loops never ended (item hold-and-press routine, stand summon, tool-use dialogue wait).
25. **Player members decide the upvalue.** A copy `X = R` whose next line reads a Player-only member (`Character`, `PlayerGui`, `Backpack`, `CharacterAdded`, `PlayerStats`, ...) is the enclosing player variable when the function's own R cannot be a Player there: its latest write is a literal, a function, one of several call results, or a `FindFirstChildOfClass/WhichIsA` (other class) / `FindFirstChild` (literal name) result, or R is used as a part (`R.CFrame = ...`) in between, or the closure also copies R into a CFrame (it then holds two upvalues of that name). travelToInstant's item-stick Heartbeat callback read `.Character` of the Humanoid (error every frame, the character was never held at the item).
26. **Else on a trailing guarded call.** `if a then X; if g then g() end else Y; if g then g() end end` prints with the else on the guard: `if g then g() else Y; if g then g() end end`. The second test of g where g is known nil is the signature (Pass 16b). In travelToInstant the Tween branch was only reachable when `recordTeleport` was nil, so Tween travel never moved and Teleport always snapped; two sell notifications never showed.
27. **Materialized booleans consumed at once.** `setSetting("K", v == "Teleport")` / `getgenv().flag = v == "Teleport"` print as rule 6 with the loaded register not in the condition; Pass 1b accepts a load the next statement consumes as a call argument or field store (Farm Travel Method dropdown: switching back to Teleport did nothing).
28. **Call base after argument-table setup (13b).** A register reused as a call base (`R = Window.GlobalSetting` ... `R(Window, opts)`) shows the earlier local in R is dead even when the setup in between stores into the options table and defines a Callback closure (up to 80 lines). Missed before: the floating toggle button's drag closures read the button through main register 149 after it was reused (`attempt to index nil with 'X'` when dragging).
29. **Or-chain body that calls the tested register.** Pass 3b's relaxed rule rejected a REST that starts by assigning the final test's register (`x = x or v` operands for Pass 4). A REST whose second statement is a bare call of that register (`X = cleanup / X()`) is a body, not an operand: `if not ok or not isItemAvailable(m) then cleanup(); return false end` in collectSingleItemNormal and `if not tweenOk or abort() then ... return false end` in travelToInstant. As printed, a failed travel went on to fire the item prompt from wherever the character was.
17. **Register reuse evidence (13b).** A main-chunk register R captured by a closure is reused when its next write changes the value kind (copies take their source's kind; a function replacing a function counts), when a later call is based at or below R (calls sit at the stack top), when the next write sets up an argument of a call based below R, when the closure indexes R as an object and R later gets a string/number/boolean, or when R later gets a function literal and did not hold one (e.g. `lastTeleportTick` in register 89, later reused for the farm activity check). The split range starts at the latest write in the closure's own block, so conditional writes in between stay consistent.

## Passes in fix_deob.py (current counts)
| Pass | What | Count |
|------|------|-------|
| 0b | Structural repairs (loop end, bypass else, GameModes or-chain, outer game-ready while, findNearestItem post-loop tail) | 5 |
| 0d | Lost gotos: `F = true` at the goto, skipped code wrapped in `if not F then`, `if F then break end` where a loop is left; anchored overrides for `continue` gotos (GOTO_CONTINUE) and gotos to the join after an if/else (GOTO_JOIN, findNearestItem's priority chain) | 44 resolved, 127 left (46 of them inside the chain Pass 15 replaces) |
| 0c | Double while loops collapsed / post-loop code moved out | 49 / 22 |
| 0 | TESTSET restoration (marked / operands moved / ternary else); sources written as one of several call results count as own registers; and-chain tails left to 3b (rule 22) | 388 / 32 / 11 |
| 1 | Truthiness inversions (TEST) | 5,396 |
| 1b | Boolean materializations (incl. loads consumed by the next statement, rule 27) | 54 |
| 2 | Setlist bugs | 198 fixed, 66 skipped |
| 2b | Multi-return forwarding | 210 |
| 3 | Targeted patches (namecall varargs, game-ready break, standfarm bridge, first-spawn loop) | 10 |
| 3a | Or-of-and chains with a mid-chain empty test (busy checks, auto-sell Roka wait, server-hop decisions) | 8 |
| 3b | Or-chains, including an else on the outer if (rule 21) and callee-first bodies (rule 29) | 782 |
| 4 | Dropped right operands and guarded call bodies (register tests and comparisons) | 186 |
| 4b | Constant ternaries (else restored) | 82 |
| 5 | Varargs passthrough wrappers | 98 |
| 6 | Timeout breaks | 4 |
| 7 | Branding replacements | 48 |
| 8 | Key system stubs | 1 |
| 8b | While-condition chains | 106 |
| 8c | Loop tail exits (yields moved / success breaks) | 39 / 32 |
| 8d | Repeat-until-or exits (rule 24) | 5 |
| 9 | Remaining while-true guard breaks + safety yields | 2 + 14 |
| 10 | Players service upvalue fixes | 75 |
| 11 | Dropped return-false | 0 |
| 12 | Game-ready initial skip (structure already correct after 0c/0d) | 0 |
| 13 | Scope leaks (186 renamed for collisions, incl. forced upvalues, dead lexical owners and Player-member evidence (rule 25); copy writes read earlier in their loop are loop-carried, so own) | 1,695 |
| 13d | Parameters shadowing an enclosing parameter renamed (`handleSound(model, sound)` callbacks) | 5 |
| 13e | Recursive local functions (rule 23) | 2 |
| 13c | AutoPlay stage-click log copy (anchored patch) | 1 |
| 13b | Closed upvalues (main-chunk registers split per captured range) | 315 |
| 14 | Garbled empty strings | 220 |
| 16 | Misplaced else (Server Hop Farm toggle off path) | 1 |
| 16b | Else on a trailing guarded call (rule 26) | 3 |
| 17 | And-chains with a misattached else (findNearestItem non-farmable skip, prestige) | 3 |
| 18 | findNearestItem selection match (inverted: selecting an item matched everything else) | 1 |
| 15 | Item-name normalizer chain replaced by a first-match loop over 47 rules read from the raw output | 47 |

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
With FIRE set, repeated output lines are counted (log spam shows as `N x line`). `ITEMS=Diamond,Rokakaka,...` creates item models (ClassName Model, a Handle, a ProximityPrompt with that ObjectText) under `Workspace.Item_Spawns.Items`; for farm runs add `DialogueGui` to ABSENT (mock GUIs are enabled and visible, so `isBusy()` would always be true). `COVER=<from>-<to>` prints which scr lines in that range executed (and how often). `LOG_REMOTES=1` logs every FireServer/InvokeServer with its arguments. HumanoidRootPart.CFrame writes are summarised at exit. `EVAL=<lua>` runs code after boot (main-chunk locals are reachable through `debug.getupvalue` on functions in `_G.YBAItemFarm`); every Teleport/TeleportToPlaceInstance call is logged with its scr call stack. Mock limitations: every child exists unless listed in ABSENT, numeric properties (`.X`, `.Value`) return numbers (so `UDim2.X.Scale` errors in the UI position saver and `PlayerStats.Quest.Value` is a number), `.Parent` is the creating proxy, `IsA` is true except for ITEMS models, `ProximityPrompt:InputHoldBegin/End` are not callable, MacLib is a stub, and collected items never disappear (the farm reports "Stuck on item" after 8 s).

## Current Status
- Builds 8d707ab, dbd97d4, 253ad8a: see git log; their reported errors are reproduced and cleared in the harness.
- Round 3 report (253ad8a): watermark `RenderStepped` on a ScreenGui (32513), `GetPlayerFromCharacter` nil (41841/41924), `Destroy` on nil (52419/52503), repeating `number < function` (56130). Causes: service-member collision, for-loop control register read, TESTSET false positive, main register 160 reused as an `ipairs` generator. All fixed (rules 12, 16, 17).
- Item farm toggle ("spams logs and does not work"): the watchdog's `if status == "dead"` lost its body, so every 2 s it warned and spawned another farm thread; the farm loop itself hit a nil multiplier (lost ternary), spun without yielding (double while), and had 7 lost gotos. All fixed; in the harness the farm runs with no errors and a constant task count.
- Round 4 report (113513c): `scr:29544 attempt to call a number value` in the item farm watchdog. Main register 89 held `lastTeleportTick` (written by the teleport marker closure) and was then reused for the farm activity function, so the first teleport overwrote it. Fixed by the function-literal rule above. The same watchdog lost its `and`: the stall recovery (no farm action for 8 s) only ran when no farm thread existed; now `if thread and status == "dead" then resurrect else stall check end`.
- Round 5 report (d36932e): `Lib.Spawn Error: attempt to yield across metamethod/C-call boundary | ClientFunctions 1354` while loading into a server. Printed by the game's own ClientFunctions; nothing in scr references it, and scr's only metamethod hooks are installed after the game-ready wait. If it ever appears after the UI loads, test with `YBA/NO_HOOKS.txt` (skips the bypass hooks) to see whether the executor cannot yield through the `newcclosure` __namecall hook.
- Item farm item selection: `normalizeItemName` returned "Yellow Candy" for almost every item and plain "Candy" for every colored candy (or-of-and chains misread + 46 lost gotos), so selected farm items never matched. Rebuilt (Pass 15); all 47 canonical names map to themselves in the harness.
- Also fixed: Reconnect button passed the JobId as the player; 6 ProximityPrompt `FireServer("PromptTriggered", ...)` calls passed the string/method instead of the prompt; VirtualInputManager `SendKeyEvent` passed the manager as the key; a `LearnSkill` call used the string "II" as the function; AutoPlay stage-click log.
- Round 6 report (c0cccd9): "it server hops, doesn't teleport to items, just teleports around and then server hops". Causes: `findNearestItem`'s selection test was inverted and its candidate code sat in an else (Pass 17/18), its priority goto and post-loop tail were lost (0d join, 0b); `isBusy()` returned true whenever its first flag was clear (rule 20), so `collectSingleItemNormal` and travel refused every item and the farm roamed until the no-item server hop; the auto-sell Roka wait never ended and spun without yielding after 2 s; the item hold-and-press routine had two exit-less loops (rule 24). In the harness the farm now picks the Lucky Arrow first, writes the HRP CFrame toward it and fires its prompt RemoteEvent.
- Round 7 report (b6cfe2d): kicked ("No exploiting! (bruh)") right after starting the item farm, with `Character is not a valid member of Humanoid` at scr 21969. The farm now reaches items, which exposed travelToInstant: its stick callback read `.Character` of the Humanoid (rule 25), and its Tween branch was unreachable (rule 26), so travel always snapped the character straight to the item (Farm Travel Method defaults to "Teleport" when no setting is saved; `_YBA_directTP` true). The dropdown could not switch back to Teleport (rule 27). The kick itself is the game's server reacting to the movement; Tween travel (425 studs/s, at most 3 s per trip, original design) is the gentler option.
- Round 8 report (56193fd): kicked ("No exploiting! (#NoNoNo)") after starting the farm; `attempt to index nil with 'X'` at scr 30307/30555 when dragging the floating toggle button (rule 28). collectSingleItemNormal and travelToInstant treated a failed travel as success and kept firing the prompt from a distance (rule 29), the likely cause of the kick.
- Watchdog: `if not normalThread or coroutine.status(normalThread) == "dead" then resurrect else stall check end` (rule 21 reading; the resurrect function assigns `normalThread`, so it cannot loop). The round 4 note read it as `thread and dead`.
- `Lib.Spawn Error: attempt to yield across metamethod/C-call boundary` (round 5): the user's log shows it 6 s after the script started, so scr's `__namecall` newcclosure hook (anti-teleport bypass, forwards to `oldNc`) is a plausible cause when the executor's newcclosure cannot yield. Test: create `YBA/NO_HOOKS.txt` or set `getgenv().RAILHUB_NO_HOOKS = true` before loading.
- Harness, current build: in-game and main-menu boot complete (8 tabs, 62 toggles); firing all toggles/buttons leaves only mock artifacts; farm run with items: no task errors.

## Known Residual Issues
- **81 lost gotos still fall through** (127 minus the 46 in the replaced normalizer chain): no join point within the accepted count tolerance. Larger groups: stand/shiny/rib farm helpers, the server-hop page loop, the key-type chain (key system is stubbed).
- **14 `while true do` loops without an exit** (Pass 9 adds `task.wait(0)`): some are real background loops (auto-sell max, quest watcher), others lost a `while A do` / `repeat until` exit without a timeout to recognise them (item ESP refresh loop `while cfg.enabled do ... end; cfg.loopRunning = false`, the stand-farm stand-change wait, `handleproj`). Find them with `grep -n -A1 'while true do' yba_fixed.lua | grep -B1 'task.wait(0)'`.
- Server Hop Farm "enabled" checks (`_r165`/`_r159` in the hop section) evaluate `(fm.serverHopFarmEnabled and fm.serverHopFarmEnabled()) or (getSetting and getSetting("SHF", true)) or true` by the raw TEST senses, i.e. always true when the module function is absent. That reads as the original script's `a and b or c` pitfall; left as is (not a decompile error), so "No Rokakaka found" always hops.
- Auto-skills `SKILLS[player.PlayerStats.Stand.Value]` (around `"autoSkills"`): the player is read through the function's own register holding the skills table (copied from another register, so rule 25 has no evidence).
- `task.wait(hold > 0 and hold or 0.05)` in the prompt hold helper and `travelToInstant`'s character fallback (`player.Character or player` when the first player reference is nil) are and-or ternaries with the else lost; both are unreachable in practice (hold is clamped to > 0 just before; the first reference is LocalPlayer).
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
