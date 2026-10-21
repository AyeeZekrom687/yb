# YBA Hub Port Handoff

## Project Summary
Port of the obfuscated YBA (Your Bizarre Adventure) exploit hub to a readable script. The hub
payload (`payload_obfuscated.lua`, loaded by `obfuscated.lua` after its key check) is a custom
virtual machine running encrypted bytecode. The VM is a re-encoded Lua 5.1 machine, so its
functions were dumped, lifted 1:1 to Lua 5.1 bytecode, and decompiled with unluac plus bytecode
transforms that work around unluac's bugs. The decompile is verified against the original
(see Verification). `scr` is that decompile with the hub's own backend stripped.

## Repository
- GitHub: `AyeeZekrom687/yb` (branch: `main`, file: `scr`)
- Load URL: `loadstring(game:HttpGet("https://raw.githubusercontent.com/AyeeZekrom687/yb/main/scr?t=" .. tostring(tick())))()`

## Files
- `obfuscated.lua`, `payload_obfuscated.lua` - the original scripts (reference; the payload is the hub)
- `payload_decompiled.lua` - verified decompile of the payload (generated, do not edit by hand)
- `tools/port.py` - builds `scr`: console logger on top + decompile with the port edits below
- `console_logger.lua` - mirrors the client console to `YBA/console_log.txt` (user request)
- `coordinator.lua` - feature coordinator module (user request), inserted by `tools/port.py`
- `scr` - the loaded script
- `tools/harness.lua` - Lua 5.1 mock Roblox/executor environment (see Testing)
- `tools/devirt/` - the devirtualizer/decompiler pipeline (see below)
- Superseded, kept for reference: `fix_deob.py`, `yba_hub_deobfuscated.lua` (the heuristic
  patcher over an older, buggy decompile; it had known residual errors and is replaced by the
  verified decompile)

## Build (MUST follow every time)
```bash
set -o pipefail
python3 -I tools/port.py payload_decompiled.lua console_logger.lua coordinator.lua scr
luac-250 -p scr                 # Lua 5.1 built with LUAI_MAXVARS 250 (main has >200 locals in 5.1 counting)
luau-compile --null scr         # real Luau compiler (github.com/luau-lang/luau releases)
git add scr tools/port.py coordinator.lua && git commit -m "..." && git push -u origin main
```
Every edit in `tools/port.py` is anchored on exact text and asserts its match count.

## Port edits (tools/port.py)
Only the hub's own backend is changed; all Roblox remote calls are untouched.
1. Folder `RAILhubYBA` -> `YBA` (settings, boot log; same folder as the old port and the logger).
2. Duplicate-run guard removed (`getgenv().RAILhub_YBA_Loaded` + HUD visible -> return). The
   runtime's own `__YBA_RUNTIME.Shutdown` still replaces a previous instance.
3. Auto-reload `L95_1` stubbed: it queued the paid-key loader (`AZX0OZ/keyrh`) with
   `queue_on_teleport` and wrote `autoexec/railhub.lua`. The AutoExecute setting is still stored.
4. Auto Prestige Worthiness (bug fix, user report: arrows used without Worthiness after the first
   stand). Its per-step skill routine `L44_2` spent every point on Agility/Vitality/Sturdiness I-V
   before Worthiness, so after a prestige reset the tree the next stand's arrows had no
   Worthiness; while there is no stand it now learns Agility I-III + Worthiness first. Its
   get-stand routine `L30_2` learned Worthiness up front only when the character lacked the
   `WorthinessLearned` attribute (set even when learning fails, survives a prestige); it now
   always learns before using arrows. Harness: `VALUES="Stand=None,Level=1"` shows the new order.
Kept as in the original: safe mode (`YBA/NO_AUTOSTART.txt`), headless mode (`YBA/NO_UI.txt`), the
key-info panel (reads key metadata the loader leaves in globals; no network), webhooks, server hop.
The payload contains no HWID or heartbeat calls (those are in the loader, which is not shipped).

## Regenerating the decompile (tools/devirt)
```bash
# unluac: download unluac_2025_12_23.jar into tools/devirt/unluac/, then
javac -cp tools/devirt/unluac/unluac_2025_12_23.jar -d tools/devirt/unluac/driver tools/devirt/unluac/driver/*.java
python3 -I tools/devirt/pipeline.py tools/devirt/lifted.luac /tmp/work   # -> /tmp/work/decompiled.lua (~40 s)
```
`tools/devirt/lifted.luac` is committed. It was made by replacing the VM entry call in
`payload_obfuscated.lua` with `dump_inject.lua` (walks the proto tree with the VM's own decoders,
writes `protos.jsonl`), then `lift.py protos.jsonl lifted.luac`.

Pipeline stages (`pipeline.py`):
1. `roundtrip.strip` - drop debug info.
2. `unthread.py` - undo jump threading into loop back edges (canonical end-of-body targets).
3. `chains.py` - unluac mis-decompiles mixed and/or value chains computed into a temp
   (`a or (b and c)` printed as `not a and b and c`); rewrite them into the TESTSET r r+1 form
   unluac handles. Value tests require the tested value to be computed right before the TEST.
4. `doonce.py` - `continue` and skip-the-rest-of-block jumps (Luau `continue`, `repeat ... until
   true`) become `while true do ... break end`. Targets come from unluac's own label report and
   from `jump_into_else`, which finds jumps unluac would silently turn into a wrong if-else:
   a test after statements of the supposed then-block aiming at the else start, or a jump from
   before the else block to a join inside it.
5. `locinfer.py` - infer locals (debug info) from luac's register allocation invariants.
6. unluac in no-goto mode (`unluac/driver/Run.java`), then `srcfix.py`.

## Verification (all must hold after any pipeline change)
```bash
python3 -I tools/devirt/verify.py tools/devirt/lifted.luac payload_decompiled.lua luac-250   # recompile, compare
luac-250 -s -o rec.luac payload_decompiled.lua
python3 -I tools/devirt/equiv.py tools/devirt/lifted.luac rec.luac                            # CFG equivalence
```
Current: 1303 functions; 1277 recompile byte-identical, the rest differ only in NEWTABLE size
hints or the inserted do-once loops; `equiv.py` reports 0 non-equivalent functions (it pairs
every non-jump instruction of both control-flow graphs; tests compared by their true/false
successors; it caught 280/282 random mutations). The lifted chunk itself produces traces
byte-identical to the VM payload in the harness, and so does the decompile (4 scenarios below).

## Testing (tools/harness.lua)
Runs a script under Lua 5.1 with mocked `game`/Instances/services, a `task.*` scheduler, executor
API, in-memory filesystem and MacLib. Deterministic: simulated clock, `os.time`/`os.date` from it,
`math.randomseed(12345)`, frame callbacks in connect order. Use the 250-locals Lua build for `scr`.
```bash
curl -sS -o maclib.lua https://raw.githubusercontent.com/AZX0OZ/maclib/refs/heads/main/maclib
A="LoadingScreen1,LoadingScreen,BlackScreen,Main Menu"
SANDBOX=1 TRACE=t.txt MACLIB_PATH=maclib.lua lua-250 tools/harness.lua scr 800000000                    # menu
SANDBOX=1 TRACE=t.txt MACLIB_PATH=maclib.lua ABSENT="$A" lua-250 tools/harness.lua scr 800000000        # in game
... ABSENT="$A" FIRE="*" ...                                                                             # all toggles/buttons
... ABSENT="$A,DialogueGui" ITEMS="Mysterious Arrow,Rokakaka,Pure Rokakaka,Gold Coin" FIRE=farm FIRE_STEPS=240   # item farm
```
`TRACE` writes a canonical event trace (compare runs of two scripts with `sort` + diff after
masking wall-clock times). Other options: `FIRE`/`FIRE_STEPS`, `ITEMS`, `COVER=<from>-<to>`,
`LOG_REMOTES=1`, `EVAL=<lua>`, `VALUES=Stand=None,Level=2` (the `.Value` of instances with those names). Mock limits: every child exists unless in ABSENT, numeric
properties are numbers, `IsA` is true except for ITEMS models, collected items never disappear.
Expected harness errors (same in the original): `require` is nil, `UDim2.X.Scale` on a number,
`debug` nil in the sandbox.

## Coordinator (coordinator.lua, user request)
The original's character-driving features do not know about each other (item farm, stand farm
which teleports back to its spot every iteration, quest/NPC farm, auto prestige), so running two
makes them fight over the character. The coordinator is a cooperative lease: each feature calls
`getgenv()._YBA_Coord.checkpoint(name, isOn, idle, onPause, onWait)` at safe points, and only the
holder proceeds. With one feature on, checkpoint returns at once (harness: item-farm-only and
quest-farm-only traces are identical to the build without it, apart from one console line).
- Turns: the holder keeps the character for the turn length (Settings > Coordinator, 60 s
  default), then hands over at its next checkpoint to the feature that has waited longest. A
  feature with nothing to do (item farm with no items: roam loop; NPC farm with no target) hands
  over at once.
- Quest/NPC farm yields to Auto Prestige while it runs (both use the game's single quest slot).
- Checkpoints: item farm per-item loop and roam loop (pausing stops its tween and item stick; the
  stall watchdog's progress time is kept fresh while it waits); stand/rib/shiny farm loops (their
  respawn teleports only happen on the stand farm's turn); quest farm and NPC farm loops; auto
  prestige storyline step (`L53_2`), NPC dialogue (`L37_2`), kill (`L43_2`), get stand (`L30_2`).
- A holder that is switched off, or has not reached a checkpoint for 120 s, loses the lease.
- Turn changes print `[Coordinator] A -> B` (in `YBA/console_log.txt`). "Coordinate Farms" off
  restores the original free-for-all.
- Not covered: actions the item farm takes inside its own turn that leave the area (server hop
  farm, auto sell) and PvP/pilot features (manual use).

## User Directives
- Do NOT make your own additions — just port the script as-is with bug fixes only
- Client-to-Roblox-server communication (FireServer, InvokeServer, RemoteEvents) must NOT be removed — only the exploit hub's own backend (HWID, key verification, heartbeats) is stubbed
- Farm Coordinator IS explicitly requested
- Keep agent usage minimal and be efficient with token intake
- Roblox uses Luau (not standard Lua 5.1)
- The base (original) script does not get the user kicked: whatever it does is correct
- The script runs in a Roblox exploit environment: getgenv(), hookmetamethod, newcclosure, checkcaller, getcallingscript, getnamecallmethod, fireproximityprompt, firesignal
