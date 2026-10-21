"""Build scr from the verified decompile: strip the hub's own backend, add the coordinator
(user request), prepend the console logger.

usage: python3 -I tools/port.py payload_decompiled.lua console_logger.lua coordinator.lua scr

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
    # Auto Prestige bug fixes (user report: no Worthiness when using arrows after the first time).
    # Its per-step skill routine spent every point on Agility, Vitality and Sturdiness I-V before
    # Worthiness, so after a prestige reset the skill tree the next stand's arrows were used
    # without Worthiness.
    ('prestige worthiness first',
     '          L14_2("LearnSkill", {\n            Skill = "Stand Barrage Finisher",\n'
     '            SkillTreeType = "Stand"\n          })\n        end\n'
     '        for L4_4, L5_4 in ipairs(L0_4) do\n          L14_2("LearnSkill", {\n'
     '            Skill = "Agility " .. L5_4,\n',
     '          L14_2("LearnSkill", {\n            Skill = "Stand Barrage Finisher",\n'
     '            SkillTreeType = "Stand"\n          })\n'
     '        else\n'
     '          -- [port fix] no stand yet: the next arrow needs Worthiness, so it gets the skill\n'
     '          -- points first (after Agility I-III, the order sfLearnWorthiness uses)\n'
     '          for L4_4, L5_4 in ipairs({"Agility I", "Agility II", "Agility III", "Worthiness"}) do\n'
     '            L14_2("LearnSkill", {Skill = L5_4, SkillTreeType = "Character"})\n'
     '          end\n'
     '        end\n'
     '        for L4_4, L5_4 in ipairs(L0_4) do\n          L14_2("LearnSkill", {\n'
     '            Skill = "Agility " .. L5_4,\n', 1),
    # its get-stand routine learned Worthiness up front only once per character model (the
    # WorthinessLearned attribute is set even when learning fails, and survives a prestige)
    ('prestige worthiness before arrows',
     '        if not L5_2.Character:GetAttribute("WorthinessLearned") then\n'
     '          sfLearnWorthiness()\n          pcall(function()\n'
     '            L5_2.Character:SetAttribute("WorthinessLearned", true)\n          end)\n        end\n',
     '        -- [port fix] always learn before using arrows\n'
     '        pcall(sfLearnWorthiness, true)\n', 1),
]

# Coordinator checkpoints (coordinator.lua): (name, anchor, text inserted after the anchor's first
# line). Each site is the start of a loop iteration or of an action, where the feature holds no
# half-finished movement or dialogue.
CP = 'getgenv()._YBA_Coord.checkpoint'
SETTINGS_END = 'local function L28_1(A0_2, A1_2)\n  if L26_1[A0_2] == nil then\n    return A1_2\n  end\n  return L26_1[A0_2]\nend\n'
SITES = [
    # item farm: on/pause helpers, the per-item loop, the roam loop (idle: no item to collect);
    # while waiting it keeps the stall watchdog's progress time fresh
    ('item farm helpers', '  function normalFarm()\n',
     '    -- [port] coordinator hooks: the farm is on; stop its tween and item stick when pausing\n'
     '    local coordOn = function()\n      return normalFarmOn\n    end\n'
     '    local coordPause = function()\n      L150_1()\n      L155_1()\n    end\n'),
    ('item farm loop',
     '        while normalFarmOn do\n          if L126_1 and L126_1() then\n            normalFarmOn = false\n'
     '            break\n          end\n          if L86_1() then\n',
     f'          if not {CP}("ItemFarm", coordOn, false, coordPause, noteItemFarmProgress) then\n'
     '            break\n          end\n'),
    ('item farm roam loop',
     '          while normalFarmOn do\n            local L7_2 = false\n            if serverHopFarmEnabled then\n',
     f'            if not {CP}("ItemFarm", coordOn, true, coordPause, noteItemFarmProgress) then\n'
     '              break\n            end\n'),
    # stand farms (normal, rib, shiny): one participant
    ('stand farm loop',
     '    while L149_1.standFarmRunning do\n      local L2_2 = false\n      while true do\n        L1_2 = L1_2 + 1\n',
     f'      if not {CP}("StandFarm", function()\n'
     '        return L149_1.standFarmRunning or L149_1.ribFarmRunning or L149_1.shinyFarmRunning\n'
     '      end) then\n        break\n      end\n'),
    ('rib farm loop',
     '    while L149_1.ribFarmRunning do\n      local L7_2 = false\n      L5_2 = L5_2 + 1\n',
     f'      if not {CP}("StandFarm", function()\n'
     '        return L149_1.standFarmRunning or L149_1.ribFarmRunning or L149_1.shinyFarmRunning\n'
     '      end) then\n        break\n      end\n'),
    ('shiny farm loop',
     '    while L149_1.shinyFarmRunning do\n      local L7_2 = false\n      L5_2 = L5_2 + 1\n',
     f'      if not {CP}("StandFarm", function()\n'
     '        return L149_1.standFarmRunning or L149_1.ribFarmRunning or L149_1.shinyFarmRunning\n'
     '      end) then\n        break\n      end\n'),
    # quest farm and NPC farm (mutually exclusive in the original): one participant; the NPC
    # farm is idle when its last attack found no target
    ('quest farm loop',
     '          local L0_3 = 0\n          while L164_1 do\n            local L1_3 = L2_1:FindFirstChild("PlayerStats")\n',
     None),
    ('npc farm loop',
     '          while L165_1 do\n            local L0_3 = L194_1(L168_1)\n            if not L0_3 then\n',
     None),
    # auto prestige: each storyline step, NPC dialogue, kill, and getting a stand
    ('prestige step',
     '    local function L53_2()\n      if L5_2.PlayerStats.Level.Value == 50 and L5_2.PlayerStats.Prestige.Value >= 3 then\n',
     f'      if not {CP}("AutoPrestige", L1_2) then\n        return\n      end\n'),
    ('prestige dialogue',
     '    local function L37_2(A0_3, A1_3, A2_3, A3_3)\n      A3_3 = A3_3 or 2\n',
     f'      if not {CP}("AutoPrestige", L1_2) then\n        return false\n      end\n'),
    ('prestige kill',
     '    local function L43_2(A0_3, A1_3, A2_3, A3_3)\n      local function L4_3(A0_4)\n',
     f'      if not {CP}("AutoPrestige", L1_2) then\n        return\n      end\n'),
    ('prestige get stand',
     '    local function L30_2()\n      if L5_2.PlayerStats.Level.Value < 2 then\n',
     f'      if not {CP}("AutoPrestige", L1_2) then\n        return false\n      end\n'),
]

# whole-anchor replacements for sites that need more than an insertion
REPLACE = [
    ('quest farm loop',
     '          local L0_3 = 0\n          while L164_1 do\n            local L1_3 = L2_1:FindFirstChild("PlayerStats")\n',
     '          local L0_3 = 0\n'
     '          local qfCoordOn = function()\n            return L164_1 or L165_1\n          end\n'
     '          while L164_1 do\n'
     f'            if not {CP}("QuestFarm", qfCoordOn) then\n              break\n            end\n'
     '            local L1_3 = L2_1:FindFirstChild("PlayerStats")\n'),
    ('npc farm loop',
     '          while L165_1 do\n            local L0_3 = L194_1(L168_1)\n            if not L0_3 then\n',
     '          local npcCoordOn = function()\n            return L164_1 or L165_1\n          end\n'
     '          local npcIdle = false\n'
     '          while L165_1 do\n'
     f'            if not {CP}("QuestFarm", npcCoordOn, npcIdle) then\n              break\n            end\n'
     '            local L0_3 = L194_1(L168_1)\n            npcIdle = not L0_3\n            if not L0_3 then\n'),
    # stand farm respawn handlers teleport to the farm spot only on the stand farm's turn (the
    # farm loop teleports back by itself when its turn comes)
    ('stand farm respawn', '      if L1_3 and L149_1.standFarmRunning then\n        L163_1(L167_1)\n',
     '      if L1_3 and L149_1.standFarmRunning and getgenv()._YBA_Coord.allows("StandFarm") then\n'
     '        L163_1(L167_1)\n'),
    ('rib farm respawn', '      if L1_3 and L149_1.ribFarmRunning then\n        L159_1(L164_1)\n',
     '      if L1_3 and L149_1.ribFarmRunning and getgenv()._YBA_Coord.allows("StandFarm") then\n'
     '        L159_1(L164_1)\n'),
    ('shiny farm respawn', '      if L1_3 and L149_1.shinyFarmRunning then\n        L155_1(L160_1)\n',
     '      if L1_3 and L149_1.shinyFarmRunning and getgenv()._YBA_Coord.allows("StandFarm") then\n'
     '        L155_1(L160_1)\n'),
]

UI_ANCHOR = 'L151_1 = "Settings"\nL151_1 = L146_1[L151_1]\n'
UI = '''-- [port] Coordinator settings (user request)
do
  local C = getgenv()._YBA_Coord
  local L0_2 = L151_1:Section({Side = "Right", Name = "Coordinator"})
  L0_2:Toggle({
    Name = "Coordinate Farms",
    Default = C.enabled,
    Callback = function(A0_3)
      L27_1("COORD", A0_3)
      C.setEnabled(A0_3)
    end
  })
  local L1_2 = {"30", "60", "120", "300"}
  L0_2:Dropdown({
    Name = "Turn Length (sec)",
    Options = L1_2,
    Default = tostring(C.slice),
    Callback = function(A0_3)
      local L1_3 = tonumber(type(A0_3) == "number" and L1_2[A0_3] or A0_3)
      if L1_3 then
        C.slice = L1_3
        L27_1("COORD_SLICE", L1_3)
      end
    end
  })
end
'''


def add_coordinator(text, coord):
    """Insert the coordinator module, its checkpoints and its settings section."""
    assert text.count(SETTINGS_END) == 1, 'settings helpers'
    text = text.replace(SETTINGS_END, SETTINGS_END + '\n' + coord.rstrip('\n') + '\n')
    for name, anchor, insert in SITES:
        if insert is None:
            continue
        assert text.count(anchor) == 1, f'{name}: anchor'
        head, rest = anchor.split('\n', 1)
        text = text.replace(anchor, head + '\n' + insert + rest)
    for name, old, new in REPLACE:
        assert text.count(old) == 1, f'{name}: anchor'
        text = text.replace(old, new)
    assert text.count(UI_ANCHOR) == 1, 'settings tab'
    return text.replace(UI_ANCHOR, UI_ANCHOR + UI)


def strip_reload(text):
    """Replace the body of the auto-reload function L95_1, keeping its AutoExecute state update."""
    assert text.count(RELOAD_START) == 1, 'auto-reload start'
    a = text.index(RELOAD_START)
    b = text.index(RELOAD_END, a)
    body = text[a:b]
    assert 'queue_on_teleport' in body or 'L93_1()' in body, 'auto-reload body'
    assert 'autoexec/railhub.lua' in body, 'auto-reload autoexec write'
    return text[:a] + RELOAD_STUB + text[b:]


def main(src, loggerf, coordf, out):
    with open(src, 'r', encoding='latin-1') as f:
        text = f.read()
    for name, old, new, count in EDITS:
        found = text.count(old)
        assert found == count, f'{name}: expected {count} matches, found {found}'
        text = text.replace(old, new)
    text = strip_reload(text)
    with open(coordf, 'r', encoding='ascii') as f:
        text = add_coordinator(text, f.read())
    with open(loggerf, 'r', encoding='ascii') as f:
        logger = f.read().rstrip('\n')
    result = logger + '\n\n' + text
    with open(out, 'w', encoding='latin-1') as f:
        f.write(result)
    print(f'Written {out} ({result.count(chr(10)) + 1} lines)')


if __name__ == '__main__':
    main(*sys.argv[1:5])
