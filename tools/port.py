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

# Auto Prestige at Prestige 3 Level 50, after the original's Auto Leave on Max check
STAND_FARM_ON_MAX = (
    '      -- [port] Stand Farm on Max (user request)\n'
    '      if L28_1("StandFarmOnMax", false) and getgenv()._YBA_StartStandFarm then\n'
    '        L107_1("Auto Prestige", "Prestige 3 Level 50: starting Stand Farm")\n'
    '        pcall(getgenv()._YBA_StartStandFarm)\n'
    '      end\n')

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
    # (user report, level 3: still no Worthiness) the game unlocks Worthiness at level 4 and an
    # arrow used without it is consumed with no stand; the get-stand routine started at level 2
    # (again on every level-up from 3), used up to 3 arrows per try for 20 tries and bought more
    ('prestige arrows from level 4',
     '    local function L30_2()\n      if L5_2.PlayerStats.Level.Value < 2 then\n        return false\n      end\n',
     '    local function L30_2()\n'
     '      -- [port fix] no arrows before Worthiness can be unlocked (level 4), nor again at a level\n'
     '      -- where an arrow already gave no stand\n'
     '      local L0_3 = getgenv()._YBA_ArrowRetry\n'
     '      if L5_2.PlayerStats.Level.Value < 4 or L0_3 and L0_3.prestige == L5_2.PlayerStats.Prestige.Value'
     ' and L5_2.PlayerStats.Level.Value < L0_3.level then\n'
     '        return false\n      end\n', 1),
    ('prestige one arrow per try',
     '          L29_2(3)\n          L0_3 = L0_3 + 1\n          if L5_2.PlayerStats.Stand.Value ~= "None" then\n',
     '          L29_2(1)\n          L0_3 = L0_3 + 1\n'
     '          if L5_2.PlayerStats.Stand.Value == "None" then\n'
     '            -- [port fix] the arrow gave no stand (no Worthiness yet): stop using and buying\n'
     '            -- arrows until the next level-up\n'
     '            getgenv()._YBA_ArrowRetry = {\n'
     '              prestige = L5_2.PlayerStats.Prestige.Value,\n'
     '              level = L5_2.PlayerStats.Level.Value + 1\n'
     '            }\n'
     '            break\n          end\n'
     '          if L5_2.PlayerStats.Stand.Value ~= "None" then\n', 1),
    # (user report) NPC attacks do nothing while a tool is held (an arrow or item left equipped):
    # unequip before attacking, in the quest/NPC farm's kill loop (L194_1) and Auto Prestige's
    # attack step (inside its kill routine L43_2)
    ('quest farm attack unequips',
     '      local L37_2 = game:GetService("CollectionService")\n'
     '      if L37_2:HasTag(L6_2, "Blocking") then\n',
     '      do\n'
     '        -- [port fix] attacks do nothing while a tool is held\n'
     '        local L37_2 = L2_1.Character\n'
     '        local L38_2 = L37_2 and L37_2:FindFirstChildOfClass("Humanoid")\n'
     '        if L38_2 and L37_2:FindFirstChildOfClass("Tool") then\n'
     '          pcall(L38_2.UnequipTools, L38_2)\n'
     '        end\n'
     '      end\n'
     '      local L37_2 = game:GetService("CollectionService")\n'
     '      if L37_2:HasTag(L6_2, "Blocking") then\n', 1),
    ('prestige attack unequips',
     '        local L1_4 = L5_2.Character\n        if not L1_4 then\n          return\n        end\n'
     '        local L2_4 = L1_4:FindFirstChild("RemoteFunction") or L10_2\n',
     '        local L1_4 = L5_2.Character\n        if not L1_4 then\n          return\n        end\n'
     '        do\n'
     '          -- [port fix] attacks do nothing while a tool is held\n'
     '          local L2_4 = L1_4:FindFirstChildOfClass("Humanoid")\n'
     '          if L2_4 and L1_4:FindFirstChildOfClass("Tool") then\n'
     '            pcall(L2_4.UnequipTools, L2_4)\n'
     '          end\n'
     '        end\n'
     '        local L2_4 = L1_4:FindFirstChild("RemoteFunction") or L10_2\n', 1),
] + [
    # (user request) "Stand Farm on Max (P3 L50)" toggle under Auto Prestige: when Auto Prestige
    # reaches Prestige 3 Level 50 (or is switched on already there), switch the stand farm on.
    # Auto Leave on Max, checked first by the original, still takes precedence.
    ('start stand farm helper', '  L149_1.standFarmToggleWidget = L160_1\n',
     '  L149_1.standFarmToggleWidget = L160_1\n'
     '  -- [port] switch the stand farm on as its toggle does (for Stand Farm on Max)\n'
     '  getgenv()._YBA_StartStandFarm = function()\n'
     '    if L149_1.standFarmRunning then\n      return\n    end\n'
     '    L149_1._toggleSyncing = true\n'
     '    pcall(function()\n      L160_1:UpdateState(true)\n    end)\n'
     '    L149_1._toggleSyncing = nil\n'
     '    L27_1("SF", true)\n'
     '    L20_1.StandFarmEnabled = true\n'
     '    L149_1.farmStats = {\n      arrowsUsed = 0,\n      rokasUsed = 0,\n      ribsUsed = 0,\n'
     '      luckyArrowsUsed = 0,\n      standsGot = 0,\n      startTime = tick()\n    }\n'
     '    farmStats = L149_1.farmStats\n'
     '    L150_1()\n'
     '  end\n', 1),
    ('stand farm on max toggle',
     '  L152_1:Toggle({\n    Name = "Auto Leave on Max (P3 L50)",\n'
     '    Default = L28_1("AutoLeavePrestige", false),\n    Callback = function(A0_2)\n'
     '      L27_1("AutoLeavePrestige", A0_2)\n      if A0_2 and L127_1 then\n        L127_1()\n'
     '      end\n    end\n  })\n',
     '  L152_1:Toggle({\n    Name = "Auto Leave on Max (P3 L50)",\n'
     '    Default = L28_1("AutoLeavePrestige", false),\n    Callback = function(A0_2)\n'
     '      L27_1("AutoLeavePrestige", A0_2)\n      if A0_2 and L127_1 then\n        L127_1()\n'
     '      end\n    end\n  })\n'
     '  -- [port] (user request) Auto Prestige switches the stand farm on at Prestige 3 Level 50\n'
     '  L152_1:Toggle({\n    Name = "Stand Farm on Max (P3 L50)",\n'
     '    Default = L28_1("StandFarmOnMax", false),\n    Callback = function(A0_2)\n'
     '      L27_1("StandFarmOnMax", A0_2)\n    end\n  })\n', 1),
    ('stand farm on max (already max)',
     '      getgenv().AutoPrestigeEnvBackup = nil\n      if L127_1 and L127_1() then\n'
     '        return\n      end\n',
     '      getgenv().AutoPrestigeEnvBackup = nil\n      if L127_1 and L127_1() then\n'
     '        return\n      end\n' + STAND_FARM_ON_MAX, 1),
    ('stand farm on max (reached max)',
     '        delfile("AutoPres3_" .. L5_2.Name .. ".txt")\n      end)\n'
     '      if L127_1 and L127_1() then\n        return false\n      end\n',
     '        delfile("AutoPres3_" .. L5_2.Name .. ".txt")\n      end)\n'
     '      if L127_1 and L127_1() then\n        return false\n      end\n' + STAND_FARM_ON_MAX, 1),
] + [
    # (user request) RAILhub branding -> "YBA Thing": everything shown in the UI, notifications,
    # console and webhooks, and the names of the script's own GUI/ESP instances. Internal getgenv
    # flags, the key loader's data and download URLs keep their names (not shown anywhere).
    ('brand: name', 'RAIL Hub', 'YBA Thing', 29),
    ('brand: warn prefix', '[RAILhub]', '[YBA Thing]', 1),
    ('brand: boot log', 'RAILhub UI & features', 'YBA Thing UI & features', 1),
    ('brand: window title', 'Title = "RailHub"', 'Title = "YBA Thing"', 1),
    ('brand: title fallback', '"RAIL|hub"', '"YBA Thing"', 2),
    ('brand: watermark', '"RailHub | %s', '"YBA Thing | %s', 1),
    ('brand: toggle gui', '"RAILhubToggle"', '"YBAThingToggle"', 2),
    ('brand: watermark gui', '"RAILhubWatermark"', '"YBAThingWatermark"', 2),
    ('brand: staff esp', '"RAILhubStaffESP"', '"YBAThingStaffESP"', 1),
    ('brand: staff label', '"RAILhubStaffLabel"', '"YBAThingStaffLabel"', 1),
    ('brand: player esp', '"RAILhubPlayerESP"', '"YBAThingPlayerESP"', 1),
    ('brand: player label', '"RAILhubPlayerLabel"', '"YBAThingPlayerLabel"', 1),
    ('brand: item esp', '"RailHubItemESP"', '"YBAThingItemESP"', 1),
    ('brand: shiny sound', '"RAILhubShinySound"', '"YBAThingShinySound"', 1),
    # (user report: webhook filters off, still pinged for item drops) the webhook settings were
    # loaded as `get(key, true) or true`, so a toggle saved off was on again after every
    # rejoin/hop/re-execute (the UI, which reads the setting itself, still showed it off)
    ('webhook enabled load',
     'L109_1.Enabled = L28_1 and L28_1("WH_Enabled", true) or L28_1 and L28_1("WH_PingRare", true) or true\n',
     '-- [port fix] read saved webhook toggles as saved (`get(k, true) or true` made "off" on)\n'
     'L109_1.Enabled = L28_1("WH_Enabled", true) ~= false\n', 1),
    ('webhook hide user load', 'L109_1.HideUser = L28_1 and L28_1("WH_HideUser", false) or false\n',
     'L109_1.HideUser = L28_1("WH_HideUser", false) == true\n', 1),
] + [
    (f'webhook {k} load', f'L109_1.Notify{k} = L28_1 and L28_1("WH_Notify{k}", true) or true\n',
     f'L109_1.Notify{k} = L28_1("WH_Notify{k}", true) ~= false\n', 1)
    for k in ('Item', 'Stand', 'Prestige', 'Staff', 'Kick')
] + [
    # Auto Prestige posted its started/stopped embeds straight to the webhook URL, ignoring the
    # webhook switch and the Auto Prestige Progress filter
    ('webhook prestige sender', '  local function L157_1(A0_2)\n    local L1_2 = getgenv().webhook\n',
     '  local function L157_1(A0_2)\n'
     '    -- [port fix] respect the webhook switch and the Auto Prestige Progress filter\n'
     '    if not L109_1.Enabled or not L109_1.NotifyPrestige then\n      return\n    end\n'
     '    local L1_2 = getgenv().webhook\n', 1),
    # (user request) stand farm method "Arrow + Lucky Arrow": Lucky Arrows first, then
    # Mysterious Arrows; collects Rokakaka, Mysterious and Lucky Arrows from the map; never buys
    # Lucky Arrows (only the "Lucky Arrow" method does)
    ('combined arrows: dropdown',
     '    Options = {\n      "Arrow",\n      "Lucky Arrow",\n      "Rib Cage"\n    },\n'
     '    Default = L28_1("SFM", "Arrow"),\n',
     '    Options = {\n      "Arrow",\n      "Lucky Arrow",\n      "Arrow + Lucky Arrow",\n'
     '      "Rib Cage"\n    },\n    Default = L28_1("SFM", "Arrow"),\n', 1),
    ('combined arrows: usable now',
     '          elseif L17_2 == "Rib Cage" then\n            if L8_2 and 0 < L10_2 then\n'
     '              L16_2 = true\n            elseif not L8_2 and 0 < L11_2 then\n'
     '              L16_2 = true\n            end\n          end\n',
     '          elseif L17_2 == "Rib Cage" then\n            if L8_2 and 0 < L10_2 then\n'
     '              L16_2 = true\n            elseif not L8_2 and 0 < L11_2 then\n'
     '              L16_2 = true\n            end\n'
     '          elseif L17_2 == "Arrow + Lucky Arrow" then\n'
     '            -- [port] (user request) either arrow kind\n'
     '            if L8_2 and 0 < L10_2 then\n              L16_2 = true\n'
     '            elseif not L8_2 and (0 < L9_2 or 0 < L12_2) then\n              L16_2 = true\n'
     '            end\n          end\n', 1),
    ('combined arrows: needed items',
     '              if L3_3 < L8_3 then\n'
     '                table.insert(L0_3, "Rib Cage of The Saint\'s Corpse")\n'
     '              end\n            end\n            return L0_3\n',
     '              if L3_3 < L8_3 then\n'
     '                table.insert(L0_3, "Rib Cage of The Saint\'s Corpse")\n'
     '              end\n'
     '            elseif L5_3 == "Arrow + Lucky Arrow" then\n'
     '              if L2_3 < L6_3 then\n                table.insert(L0_3, "Rokakaka")\n              end\n'
     '              if L1_3 < L7_3 then\n                table.insert(L0_3, "Mysterious Arrow")\n'
     '              end\n'
     '            end\n            return L0_3\n', 1),
    ('combined arrows: collected items',
     '            elseif L0_3 == "Rib Cage" then\n              return {\n                "Rokakaka",\n'
     '                "Rib Cage of The Saint\'s Corpse"\n              }\n            end\n',
     '            elseif L0_3 == "Rib Cage" then\n              return {\n                "Rokakaka",\n'
     '                "Rib Cage of The Saint\'s Corpse"\n              }\n'
     '            elseif L0_3 == "Arrow + Lucky Arrow" then\n'
     '              return {\n                "Rokakaka",\n                "Mysterious Arrow",\n'
     '                "Lucky Arrow"\n              }\n'
     '            end\n', 1),
    ('combined arrows: lucky first',
     '      if 0 < L11_2 and L149_1.farmMethod == "Lucky Arrow" then\n',
     '      if 0 < L11_2 and (L149_1.farmMethod == "Lucky Arrow"'
     ' or L149_1.farmMethod == "Arrow + Lucky Arrow") then\n', 1),
    # (user request) lag reductions that keep speed and behaviour:
    # the name lowercaser ran one pcall closure + gsub per Cyrillic capital (33) on every call;
    # one gsub over the same table gives the same result (each key is \208 + one byte, no value
    # is a key), and a bounded cache serves the names the item farm checks again and again
    ('perf: lowercaser',
     'local function L141_1(A0_2)\n  if not A0_2 then\n    return ""\n  end\n'
     '  local L1_2 = tostring(A0_2):lower()\n  if type(L140_1) == "table" then\n'
     '    for L5_2, L6_2 in pairs(L140_1) do\n'
     '      if type(L5_2) == "string" and type(L6_2) == "string" and L6_2 ~= "" then\n'
     '        pcall(function()\n          L1_2 = L1_2:gsub(L5_2, L6_2)\n        end)\n'
     '      end\n    end\n  end\n  return L1_2\nend\n',
     'local L141_1\n'
     'do\n'
     '  -- [port perf] one gsub over the letter table (was one pcall + gsub per letter), cached\n'
     '  local L0_2, L1_2 = {}, 0\n'
     '  function L141_1(A0_2)\n'
     '    if not A0_2 then\n      return ""\n    end\n'
     '    local L2_2 = tostring(A0_2)\n'
     '    local L3_2 = L0_2[L2_2]\n'
     '    if L3_2 then\n      return L3_2\n    end\n'
     '    L3_2 = L2_2:lower():gsub("\\208[\\128-\\191]", L140_1)\n'
     '    if 4096 <= L1_2 then\n      L0_2, L1_2 = {}, 0\n    end\n'
     '    L0_2[L2_2] = L3_2\n    L1_2 = L1_2 + 1\n'
     '    return L3_2\n  end\nend\n', 1),
    # the key-info panel (RAILhub key type/expiry under the menu header; the port has no key
    # system) refreshed every second: ~20 pcall'd global lookups and up to 3 reads + JSON
    # decodes of the loader's key file per refresh. Not created any more.
    ('perf: key panel',
     '  task.spawn(function()\n    for L3_2 = 1, 40 do\n      if L168_1() then\n        break\n'
     '      end\n      task.wait(0.2)\n    end\n    L169_1()\n    while true do\n'
     '      if not task.wait(1) or not pcall(L169_1) then\n        break\n      end\n    end\n'
     '  end)\n',
     '  -- [port] RAILhub key panel (key type / expiry) not created: no key system in the port,\n'
     '  -- and its once-per-second refresh read the key file and ~20 globals\n', 1),
    # Auto Prestige's kill noclip rebuilt its part list with GetDescendants on every physics
    # step once the character died mid-fight (the DescendantAdded watcher died with it)
    ('perf: prestige kill noclip',
     '              for L3_5, L4_5 in ipairs(L5_2.Character:GetDescendants()) do\n'
     '                if L4_5:IsA("BasePart") then\n                  L24_3[#L24_3 + 1] = L4_5\n'
     '                end\n              end\n            end)\n          end\n'
     '          for L3_4 = 1, #L24_3 do\n',
     '              for L3_5, L4_5 in ipairs(L5_2.Character:GetDescendants()) do\n'
     '                if L4_5:IsA("BasePart") then\n                  L24_3[#L24_3 + 1] = L4_5\n'
     '                end\n              end\n            end)\n'
     '            -- [port perf] watch the new character so this runs once per character\n'
     '            pcall(function()\n'
     '              L25_3 = L5_2.Character.DescendantAdded:Connect(function(A0_5)\n'
     '                if A0_5:IsA("BasePart") then\n                  L24_3[#L24_3 + 1] = A0_5\n'
     '                end\n              end)\n            end)\n'
     '          end\n'
     '          for L3_4 = 1, #L24_3 do\n', 1),
    # the HUD health label was rewritten every 0.1 s even when unchanged
    ('perf: hud health text', '                L2_3.Text = string.format("[%d/%d]", L5_3, L6_3)\n',
     '                local L7_3 = string.format("[%d/%d]", L5_3, L6_3)\n'
     '                if L2_3.Text ~= L7_3 then\n                  L2_3.Text = L7_3\n'
     '                end\n', 1),
    # the Info tab's Links section held only the RAILhub Discord invite
    ('brand: links section', '  local L149_1 = L147_1:Section({Side = "Right", Name = "Links"})\n',
     '  local L149_1\n', 1),
    ('brand: discord button',
     '  L149_1:Header({Text = "Links", AutoLocalize = false})\n  L149_1:Button({\n'
     '    Name = "Discord Link",\n    Callback = function()\n'
     '      L104_1("https://discord.gg/67UTqK8su5")\n    end\n  })\n',
     '', 1),
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
     '    local function L30_2()\n      -- [port fix] no arrows before Worthiness can be unlocked (level 4), nor again at a level\n',
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
