"""Build scr from the verified decompile: strip the hub's own backend, prepend the console logger.

usage: python3 -I tools/port.py decompiled.lua console_logger.lua scr

Every edit is anchored on exact text and asserts its match count, so a changed decompile fails
the build instead of silently skipping an edit. Everything not listed here is the decompiled
original unchanged (Roblox remote calls included).
"""
import sys

GUARD = '''if getgenv().RAILhub_YBA_Loaded then
  local L92_1 = L46_1 and L46_1() or L4_1
  local L93_1 = L92_1 and L92_1:FindFirstChild("HUD")
  if L93_1 and L93_1:IsA("ScreenGui") and L93_1.Enabled then
    local L94_1 = getgenv()._lastRunningWarn or 0
    if 5 < tick() - L94_1 then
      getgenv()._lastRunningWarn = tick()
      warn("[RAILhub] Script already running.")
    end
    return
  end
end
'''

RELOAD_START = 'local function L95_1(A0_2)\n'
RELOAD_END = '\nend\n\npcall(function()\n  local L0_2 = L45_1 and L45_1() or L2_1 or game:GetService("Players").LocalPlayer\n'
RELOAD_STUB = '''local function L95_1(A0_2)
  -- [port] auto-reload stubbed: it queued the paid-key loader (AZX0OZ/keyrh) for the next
  -- server with queue_on_teleport and wrote autoexec/railhub.lua
  L20_1.AutoExecute = L28_1("AE", true)'''

EDITS = [
    # (name, old, new, expected count)
    ('folder', '"RAILhubYBA', '"YBA', 6),
    ('duplicate-run guard', GUARD, '-- [port] duplicate-run guard removed\n', 1),
]


def strip_reload(text):
    """Replace the body of the auto-reload function L95_1, keeping its AutoExecute state update."""
    assert text.count(RELOAD_START) == 1, 'auto-reload start'
    a = text.index(RELOAD_START)
    b = text.index(RELOAD_END, a)
    body = text[a:b]
    assert 'queue_on_teleport' in body or 'L93_1()' in body, 'auto-reload body'
    assert 'autoexec/railhub.lua' in body, 'auto-reload autoexec write'
    return text[:a] + RELOAD_STUB + text[b:]


def main(src, loggerf, out):
    with open(src, 'r', encoding='latin-1') as f:
        text = f.read()
    for name, old, new, count in EDITS:
        found = text.count(old)
        assert found == count, f'{name}: expected {count} matches, found {found}'
        text = text.replace(old, new)
    text = strip_reload(text)
    with open(loggerf, 'r', encoding='ascii') as f:
        logger = f.read().rstrip('\n')
    result = logger + '\n\n' + text
    with open(out, 'w', encoding='latin-1') as f:
        f.write(result)
    print(f'Written {out} ({result.count(chr(10)) + 1} lines)')


if __name__ == '__main__':
    main(*sys.argv[1:4])
