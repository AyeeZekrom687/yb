"""Build scr from the verified decompile: strip the hub's own backend, add the coordinator
(user request), prepend the console logger.

usage: python3 -I tools/port.py payload_decompiled.lua console_logger.lua coordinator.lua dashboard.lua lagguard.lua scr

Every edit is anchored on exact text and asserts its match count, so a changed decompile fails
the build instead of silently skipping an edit. Everything not listed here is the decompiled
original unchanged (Roblox remote calls included).
"""
import sys

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
    # The original's duplicate-run guard stays: running the script again in the same server
    # would leave the first copy's loops, handlers and farms running (its runtime Shutdown only
    # destroys the old GUI) and stack its anti-teleport hooks, whose calls through
    # getgenv().oldNc / oldMagnitude would then loop back into the old hook.
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
    ('brand: warn prefix', '[RAILhub]', '[YBA Thing]', 2),
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
    # (user request) Lucky Arrows are never sold. The original only sells what its callers
    # check against the never-sell list (Lucky Arrow is on it); sellItem itself refuses them
    # too, so no path (auto sell, sell for Requiem/money, a sell dropdown) can sell one.
    ('never sell lucky arrows',
     '  function sellItem(A0_2, A1_2, A2_2)\n    if not A0_2 then\n'
     '      return false, "No item provided"\n    end\n',
     '  function sellItem(A0_2, A1_2, A2_2)\n    if not A0_2 then\n'
     '      return false, "No item provided"\n    end\n'
     '    do\n'
     '      -- [port] (user request) Lucky Arrows are never sold, whichever path asks\n'
     '      local L3_2 = typeof(A0_2) == "Instance" and A0_2.Name or tostring(A0_2)\n'
     '      if L143_1(L3_2) == "Lucky Arrow" then\n'
     '        return false, "Lucky Arrows are never sold"\n'
     '      end\n'
     '    end\n', 1),
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
    # Leak fixes (user request: memory leaks that build up). Auto Prestige: the session's last
    # CharacterAdded handler was never disconnected (one more on the player per session), and the
    # others only disconnected at their next event. The handler now disconnects itself, and the
    # toggle disconnects the previous session's handlers when it stops or restarts Auto Prestige.
    # A session that went stale while starting cleans up and does nothing more.
    ('leak: prestige session handlers',
     '    L59_2()\n    L5_2.CharacterAdded:Connect(function(A0_3)\n      if not L1_2() then\n'
     '        return\n      end\n      task.wait(1)\n      L59_2()\n    end)\n'
     '    L49_2(L6_2)\n    L54_2()\n  end\n',
     '    L59_2()\n'
     '    local respawnConn\n'
     '    respawnConn = L5_2.CharacterAdded:Connect(function(A0_3)\n      if not L1_2() then\n'
     '        if respawnConn then\n          respawnConn:Disconnect()\n        end\n'
     '        return\n      end\n      task.wait(1)\n      L59_2()\n    end)\n'
     '    -- [port fix] the toggle disconnects these when Auto Prestige stops or restarts\n'
     '    local function sessionCleanup()\n'
     '      for _, conn in pairs({L55_2, L56_2, L57_2, L58_2, respawnConn}) do\n'
     '        pcall(conn.Disconnect, conn)\n      end\n    end\n'
     '    if not L1_2() then\n      sessionCleanup()\n      return\n    end\n'
     '    getgenv()._YBA_APCleanup = sessionCleanup\n'
     '    L49_2(L6_2)\n    L54_2()\n  end\n', 1),
    ('leak: prestige toggle on',
     '      getgenv()._YBA_prestigeWaiting = false\n      L152_1 = L152_1 + 1\n      L27_1("AP", true)\n',
     '      getgenv()._YBA_prestigeWaiting = false\n'
     '      -- [port fix] disconnect the previous session\'s event handlers\n'
     '      if getgenv()._YBA_APCleanup then\n        pcall(getgenv()._YBA_APCleanup)\n'
     '        getgenv()._YBA_APCleanup = nil\n      end\n'
     '      L152_1 = L152_1 + 1\n      L27_1("AP", true)\n', 1),
    ('leak: prestige toggle off',
     '        getgenv()._YBA_prestigeWaiting = false\n      end\n      L152_1 = L152_1 + 1\n'
     '      L151_1 = false\n',
     '        getgenv()._YBA_prestigeWaiting = false\n      end\n'
     '      -- [port fix] disconnect this session\'s event handlers\n'
     '      if getgenv()._YBA_APCleanup then\n        pcall(getgenv()._YBA_APCleanup)\n'
     '        getgenv()._YBA_APCleanup = nil\n      end\n'
     '      L152_1 = L152_1 + 1\n      L151_1 = false\n', 1),
    # Item ESP: turning it off cleared loopRunning while the refresh loop still slept, so an
    # off/on within 2 s started a second loop next to the old one (one more per quick toggle).
    # Each enable now starts one loop and ends the previous one.
    ('leak: item esp off', '    if not A0_2 then\n      L1_2.loopRunning = false\n      return\n    end\n',
     '    if not A0_2 then\n      return\n    end\n', 1),
    ('leak: item esp loop',
     '    if not L1_2.loopRunning then\n      L1_2.loopRunning = true\n      task.spawn(function()\n'
     '        while L1_2.enabled do\n          task.wait(2)\n          if L1_2.enabled then\n'
     '            pcall(refreshItemESP)\n          end\n        end\n'
     '        L1_2.loopRunning = false\n      end)\n    end\n',
     '    -- [port fix] one refresh loop per enable; the previous one ends at its next wake\n'
     '    L1_2.loopGen = (L1_2.loopGen or 0) + 1\n'
     '    local loopGen = L1_2.loopGen\n'
     '    task.spawn(function()\n'
     '      while L1_2.enabled and L1_2.loopGen == loopGen do\n        task.wait(2)\n'
     '        if L1_2.enabled and L1_2.loopGen == loopGen then\n'
     '          pcall(refreshItemESP)\n        end\n      end\n    end)\n', 1),
    # Player ESP: same race (an off/on within 0.25 s kept the old refresh loop as well)
    ('leak: player esp loop',
     '    task.spawn(function()\n      while L1_2.enabled do\n        refreshPlayerESP()\n'
     '        task.wait(0.25)\n      end\n    end)\n  end\n',
     '    -- [port fix] one refresh loop per enable; the previous one ends at its next wake\n'
     '    L1_2.loopGen = (L1_2.loopGen or 0) + 1\n'
     '    local loopGen = L1_2.loopGen\n'
     '    task.spawn(function()\n'
     '      while L1_2.enabled and L1_2.loopGen == loopGen do\n        refreshPlayerESP()\n'
     '        task.wait(0.25)\n      end\n    end)\n  end\n', 1),
    # Noclip: the character's part list gained every part added to the character (stand
    # summons, effects) and was walked every physics step, but parts that were removed stayed
    # in it until the next character. They are now dropped every 30 steps ([port perf]: the
    # per-step check reads CanCollide first, so parts already non-colliding cost one read).
    ('leak: noclip sweep counter', '  local L146_1 = false\n  local L147_1 = {}\n  local L148_1, L149_1\n',
     '  local L146_1 = false\n  local L147_1 = {}\n  local L148_1, L149_1\n  local noclipSweep = 0\n', 1),
    ('leak: noclip part list',
     '    L151_1(A0_2)\n    for L4_2 = 1, #L147_1 do\n      local L5_2 = L147_1[L4_2]\n'
     '      if L5_2 and L5_2.Parent and L5_2.CanCollide then\n        pcall(function()\n'
     '          L5_2.CanCollide = false\n        end)\n      end\n    end\n  end\n',
     '    L151_1(A0_2)\n    for L4_2 = 1, #L147_1 do\n      local L5_2 = L147_1[L4_2]\n'
     '      if L5_2 and L5_2.CanCollide and L5_2.Parent then\n        pcall(function()\n'
     '          L5_2.CanCollide = false\n        end)\n      end\n    end\n'
     '    -- [port fix] drop parts that left the character, so the list does not grow for as\n'
     '    -- long as the character lives\n'
     '    noclipSweep = noclipSweep + 1\n'
     '    if 30 <= noclipSweep then\n      noclipSweep = 0\n'
     '      local kept = 0\n'
     '      for L4_2 = 1, #L147_1 do\n        local L5_2 = L147_1[L4_2]\n'
     '        if L5_2 and L5_2.Parent then\n          kept = kept + 1\n          L147_1[kept] = L5_2\n'
     '        end\n      end\n'
     '      for L4_2 = #L147_1, kept + 1, -1 do\n        L147_1[L4_2] = nil\n      end\n'
     '    end\n  end\n', 1),
    # Finisher changer: every RemoteEvent it hooked (each character's, for every player and
    # respawn) stayed in this table after it was destroyed
    ('leak: hooked remotes',
     '      if not getgenv().HookedRemotes then\n        getgenv().HookedRemotes = {}\n      end\n',
     '      if not getgenv().HookedRemotes then\n'
     '        -- [port fix] weak keys, so destroyed RemoteEvents are released\n'
     '        getgenv().HookedRemotes = setmetatable({}, {__mode = "k"})\n      end\n', 1),
    # (user request) Stand farm + item farm: while the item farm runs (coordinator on) and
    # collects what the stand farm's method uses, the stand farm does not collect for itself: it
    # takes a turn only with at least one arrow of its method and one Rokakaka, and otherwise
    # hands the character to the item farm (coordinator.lua feeds).
    ('stand farm: ready helper',
     '  local function L170_1()\n    return L149_1.getFarmMethod and L149_1.getFarmMethod() or L149_1.farmMethod'
     ' or L28_1 and L28_1("SFM", "Arrow") or "Arrow"\n  end\n  \n  local function L171_1()\n',
     '  local function L170_1()\n    return L149_1.getFarmMethod and L149_1.getFarmMethod() or L149_1.farmMethod'
     ' or L28_1 and L28_1("SFM", "Arrow") or "Arrow"\n  end\n  \n'
     '  -- [port] (user request) whether the item farm picks up this item (it always takes Lucky Arrows)\n'
     '  local function sfItemFarmCollects(A0_2)\n'
     '    local L1_2 = selectedFarmItems\n'
     '    if A0_2 == "Lucky Arrow" or type(L1_2) ~= "table" or #L1_2 == 0 then\n      return true\n    end\n'
     '    local L2_2 = L139_1.normalizeItemName\n'
     '    local L3_2 = L2_2 and L2_2(A0_2) or A0_2\n'
     '    for _, L4_2 in ipairs(L1_2) do\n'
     '      if L4_2 == A0_2 or L2_2 and L2_2(L4_2) == L3_2 then\n        return true\n      end\n    end\n'
     '    return false\n  end\n  \n'
     '  -- [port] (user request) false while the item farm should collect for the stand farm: it runs,\n'
     '  -- the coordinator is on, it picks up Rokakaka and the method\'s arrow, and the stand farm\n'
     '  -- lacks either (one of each lets it take its turn back)\n'
     '  local function sfCoordReady()\n'
     '    if not normalFarmOn or not getgenv()._YBA_Coord.enabled then\n      return true\n    end\n'
     '    -- L1_2: the arrows the method rolls with; the item farm must pick up the first\n'
     '    local L0_2 = L170_1()\n'
     '    local L1_2\n'
     '    if L0_2 == "Arrow" then\n      L1_2 = {"Mysterious Arrow"}\n'
     '    elseif L0_2 == "Lucky Arrow" then\n      L1_2 = {"Lucky Arrow"}\n'
     '    elseif L0_2 == "Rib Cage" then\n      L1_2 = {"Rib Cage of The Saint\'s Corpse"}\n'
     '    elseif L0_2 == "Arrow + Lucky Arrow" then\n      L1_2 = {"Mysterious Arrow", "Lucky Arrow"}\n'
     '    else\n      return true\n    end\n'
     '    if not (sfItemFarmCollects("Rokakaka") and sfItemFarmCollects(L1_2[1])) then\n'
     '      return true\n    end\n'
     '    local L3_2 = false\n'
     '    for _, L4_2 in ipairs(L1_2) do\n'
     '      L3_2 = L3_2 or 0 < (L4_2 == "Lucky Arrow" and L139_1.sfCountLucky() or L157_1(L4_2))\n'
     '    end\n'
     '    if not L3_2 and L0_2 == "Lucky Arrow" and L28_1("SF_BuyItems", false) == true then\n'
     '      -- bought from the shop in place\n'
     '      L3_2 = 75000 <= (L2_1.PlayerStats and L2_1.PlayerStats.Money.Value or 0)\n'
     '    end\n'
     '    return L3_2 and 0 < L157_1("Rokakaka")\n'
     '  end\n  \n'
     '  local function L171_1()\n', 1),
    ('stand farm: leave collecting to the item farm',
     '          local L9_2 = L157_1("Mysterious Arrow")\n          local L10_2 = L157_1("Rokakaka")\n',
     '          -- [port] (user request) the item farm collects for the stand farm while both run:\n'
     '          -- no rolls, collecting or buying here until the next checkpoint hands it over\n'
     '          if not sfCoordReady() then\n            task.wait(0.5)\n            break\n          end\n'
     '          local L9_2 = L157_1("Mysterious Arrow")\n          local L10_2 = L157_1("Rokakaka")\n', 1),
    # (user request) "Buy Arrows/Rokakaka with Money" (setting SF_BuyItems, off by default):
    # off, the stand farm (shop top-up, Lucky Arrow method), Auto Requiem's arrow for Suno and
    # Auto Prestige never buy Rokakaka or arrows; they farm them from the map instead (Auto
    # Prestige and the stand farm already fall back to that when they cannot afford them).
    ('buy toggle: saved always', 'A0_2 == "SAR" or A0_2 == "SFM" then\n    L24_1(L26_1)\n',
     'A0_2 == "SAR" or A0_2 == "SFM" or A0_2 == "SF_BuyItems" or A0_2 == "FPSCap" or A0_2 == "LowRender" or A0_2 == "LagGuard" then\n'
     '    L24_1(L26_1)\n', 1),
    ('buy toggle: stand farm lucky arrows',
     '          if L170_1() == "Lucky Arrow" and L157_1("Lucky Arrow") == 0 then\n',
     '          if L170_1() == "Lucky Arrow" and L139_1.sfCountLucky() == 0 and L28_1("SF_BuyItems", false) == true then\n', 1),
    ('buy toggle: stand farm shop top-up',
     '  function sfBuyMissingItem(A0_2)\n    if L90_1() then\n      return false\n    end\n',
     '  function sfBuyMissingItem(A0_2)\n    if L90_1() or L28_1("SF_BuyItems", false) ~= true then\n'
     '      return false\n    end\n', 1),
    ('buy toggle: requiem arrow',
     '      if 8250 <= L1_2 then\n        L170_1("Buying 1 Mysterious Arrow for Suno ($750)...")\n',
     '      if 8250 <= L1_2 and L28_1("SF_BuyItems", false) == true then\n'
     '        L170_1("Buying 1 Mysterious Arrow for Suno ($750)...")\n', 1),
    ('buy toggle: prestige rokakaka',
     '      if not L0_3 then\n        if L5_2.PlayerStats.Money.Value >= 2500 then\n',
     '      if not L0_3 then\n        if L5_2.PlayerStats.Money.Value >= 2500 and L28_1("SF_BuyItems", false) == true then\n', 1),
    ('buy toggle: prestige arrow',
     '        if not L3_3 then\n          if L5_2.PlayerStats.Money.Value >= 750 then\n',
     '        if not L3_3 then\n          if L5_2.PlayerStats.Money.Value >= 750 and L28_1("SF_BuyItems", false) == true then\n', 1),
    ('buy toggle: ui', '      L27_1("SAR", A0_2)\n    end\n  })\n',
     '      L27_1("SAR", A0_2)\n    end\n  })\n'
     '  -- [port] (user request) off: no Rokakaka or arrows bought with money (stand farm, Auto\n'
     '  -- Requiem, Auto Prestige); they are farmed from the map instead\n'
     '  L159_1:Toggle({\n'
     '    Name = "Buy Arrows/Rokakaka with Money",\n'
     '    Default = L28_1("SF_BuyItems", false),\n'
     '    Callback = function(A0_2)\n      L27_1("SF_BuyItems", A0_2)\n    end\n'
     '  })\n', 1),
    # (user request) Redeemed Lucky Arrows (bought from Jack in the Halloween event; the name
    # normalizer files them under "(Redeemed Items)", so nothing counted or used them) count as
    # Lucky Arrows for the stand farm's Lucky Arrow and Arrow + Lucky Arrow methods, and are used
    # first (they cannot be traded or sold). Buy caps stay per kind (money buy: regular ones,
    # Jack: Redeemed ones), as in the original.
    ('redeemed lucky: count helper', '  L139_1.sfCountItem = L151_1\n',
     '  L139_1.sfCountItem = L151_1\n'
     '  \n'
     '  -- [port] (user request) Lucky Arrows the stand farm rolls with: regular and Redeemed\n'
     '  function L139_1.sfCountLucky()\n'
     '    return L151_1("Lucky Arrow") + (getRedeemedLuckyCount and getRedeemedLuckyCount() or 0)\n'
     '  end\n', 1),
    ('redeemed lucky: use first',
     '      local L11_2 = L150_1("Lucky Arrow")\n'
     '      if 0 < L11_2 and (L149_1.farmMethod == "Lucky Arrow" or L149_1.farmMethod == "Arrow + Lucky Arrow") then\n'
     '        L170_1("Using Lucky Arrow (no stand)...")\n'
     '        local L12_2 = A7_2.Stand.Value\n'
     '        local L13_2 = useItemDirect("Lucky Arrow", "Lucky Arrow")\n'
     '        if not L13_2 then\n'
     '          L170_1("Lucky Arrow not found / failed to equip")\n',
     '      local L11_2 = L139_1.sfCountLucky()\n'
     '      if 0 < L11_2 and (L149_1.farmMethod == "Lucky Arrow" or L149_1.farmMethod == "Arrow + Lucky Arrow") then\n'
     '        -- [port] (user request) Redeemed Lucky Arrows first\n'
     '        local luckyName = getRedeemedLuckyCount and 0 < getRedeemedLuckyCount() and "Redeemed Lucky Arrow" or "Lucky Arrow"\n'
     '        L170_1("Using " .. luckyName .. " (no stand)...")\n'
     '        local L12_2 = A7_2.Stand.Value\n'
     '        local L13_2 = useItemDirect(luckyName, luckyName)\n'
     '        if not L13_2 then\n'
     '          L170_1(luckyName .. " not found / failed to equip")\n', 1),
    ('redeemed lucky: stand farm can roll', '          local L12_2 = L157_1("Lucky Arrow")\n',
     '          local L12_2 = L139_1.sfCountLucky()\n', 1),
    ('redeemed lucky: needed items', '            local L4_3 = L157_1("Lucky Arrow")\n',
     '            local L4_3 = L139_1.sfCountLucky()\n', 1),
    # (user request) Optimisations with identical results: the same reads, writes and remote
    # calls happen in the same cases, only fewer redundant ones.
    # Item lookup: the backpack was walked once per item on the map (sfCountItem per item); a
    # scan does not yield, so counts cannot change during it and are kept per item name.
    ('perf: item lookup counts',
     '  function findNearestItem(A0_2)\n    local L1_2 = L3_1.Character and L3_1.Character:FindFirstChild("HumanoidRootPart")\n',
     '  function findNearestItem(A0_2)\n'
     '    -- [port perf] backpack counts per item name for this scan (it does not yield)\n'
     '    local countCache = {}\n'
     '    local L1_2 = L3_1.Character and L3_1.Character:FindFirstChild("HumanoidRootPart")\n', 1),
    ('perf: item lookup count use', '          local L19_2 = L145_1 and L145_1(L18_2) or 0\n',
     '          local L19_2 = L18_2 ~= nil and countCache[L18_2]\n'
     '          if not L19_2 then\n'
     '            L19_2 = L145_1 and L145_1(L18_2) or 0\n'
     '            if L18_2 ~= nil then\n              countCache[L18_2] = L19_2\n            end\n'
     '          end\n', 1),
    # Backpack counter: "IsA("Tool") or Name" is always true (a Name is a string), so the IsA
    # call per backpack item did nothing
    ('perf: backpack count',
     '            if L11_2 and (L11_2:IsA("Tool") or L11_2.Name) and L143_1(L11_2.Name) == L1_2 then\n',
     '            if L11_2 and L143_1(L11_2.Name) == L1_2 then\n', 1),
    # PvP auto-block's workspace.DescendantAdded handler (always connected) read the local
    # character and the object's class for every object added to workspace before comparing
    # its name; the name is now compared first
    ('perf: auto-block workspace listener',
     '      for L4_2, L5_2 in ipairs(L170_1) do\n        local L6_2 = game.Players.LocalPlayer.Character\n'
     '        local L7_2 = L6_2 and (L6_2.PrimaryPart or L6_2:FindFirstChild("HumanoidRootPart"))\n'
     '        if L7_2 and A0_2:IsA("BasePart") and L5_2[1] == A0_2.Name and (L5_2[2] == "any" or L5_2[2] == A0_2.Parent) and (L7_2.Position - A0_2.Position).Magnitude < L5_2[3] then\n'
     '          reqblock(L5_2[4]())\n        end\n      end\n',
     '      -- [port perf] the object\'s name is compared before the character is read\n'
     '      local blockName = A0_2.Name\n'
     '      for L4_2, L5_2 in ipairs(L170_1) do\n'
     '        if L5_2[1] == blockName then\n'
     '          local L6_2 = game.Players.LocalPlayer.Character\n'
     '          local L7_2 = L6_2 and (L6_2.PrimaryPart or L6_2:FindFirstChild("HumanoidRootPart"))\n'
     '          if L7_2 and A0_2:IsA("BasePart") and (L5_2[2] == "any" or L5_2[2] == A0_2.Parent) and (L7_2.Position - A0_2.Position).Magnitude < L5_2[3] then\n'
     '            reqblock(L5_2[4]())\n          end\n        end\n      end\n', 1),
    # Tween and Auto Prestige kill noclip (every physics step): CanCollide is read before Parent
    ('perf: tween noclip', '        if L4_3 and L4_3.Parent and L4_3.CanCollide then\n',
     '        if L4_3 and L4_3.CanCollide and L4_3.Parent then\n', 1),
    ('perf: kill noclip', '            if L4_4 and L4_4.Parent and L4_4.CanCollide then\n',
     '            if L4_4 and L4_4.CanCollide and L4_4.Parent then\n', 1),
    # (user request) FPS cap slider in Settings (setting FPSCap, 0 = off), through the
    # executor's setfpscap. 1-9 count as 10 so a slip cannot lock the game at 1 FPS; setting it
    # back to 0 restores the cap from before (getfpscap, else Roblox's default 60).
    ('fps cap: slider', 'L152_1:Slider({\n  Name = "Menu Scale",\n',
     '-- [port] (user request) FPS cap through the executor\'s setfpscap\n'
     'do\n'
     '  local fpsCapBefore   -- the cap before this script set one, restored at 0\n'
     '  \n'
     '  local function fpsCapValue(A0_2)\n'
     '    local L1_2 = math.floor(tonumber(A0_2) or 0)\n'
     '    if L1_2 <= 0 then\n      return 0\n    end\n'
     '    return math.clamp(L1_2, 10, 240)\n'
     '  end\n'
     '  \n'
     '  local function applyFpsCap(A0_2)\n'
     '    if type(setfpscap) ~= "function" then\n      return\n    end\n'
     '    if 0 < A0_2 then\n'
     '      if fpsCapBefore == nil then\n'
     '        local L1_2, L2_2 = pcall(function()\n'
     '          return type(getfpscap) == "function" and tonumber(getfpscap()) or nil\n'
     '        end)\n'
     '        fpsCapBefore = L1_2 and L2_2 or 60\n'
     '      end\n'
     '      pcall(setfpscap, A0_2)\n'
     '    elseif fpsCapBefore ~= nil then\n'
     '      pcall(setfpscap, fpsCapBefore)\n'
     '      fpsCapBefore = nil\n'
     '    end\n'
     '  end\n'
     '  \n'
     '  L152_1:Slider({\n'
     '    Name = "FPS Cap (0 = off)",\n'
     '    Default = fpsCapValue(L28_1("FPSCap", 0)),\n'
     '    Minimum = 0,\n'
     '    Maximum = 240,\n'
     '    Precision = 0,\n'
     '    DisplayMethod = "Value",\n'
     '    Callback = function(A0_2)\n'
     '      local L1_2 = fpsCapValue(A0_2)\n'
     '      L27_1("FPSCap", L1_2)\n'
     '      applyFpsCap(L1_2)\n'
     '    end,\n'
     '    onInputComplete = function(A0_2)\n'
     '      local L1_2 = fpsCapValue(A0_2)\n'
     '      if type(setfpscap) ~= "function" then\n'
     '        L107_1("Settings", "FPS cap unavailable: this executor has no setfpscap")\n'
     '      else\n'
     '        L107_1("Settings", L1_2 == 0 and "FPS cap off" or "FPS cap: " .. L1_2)\n'
     '      end\n'
     '    end\n'
     '  })\n'
     '  applyFpsCap(fpsCapValue(L28_1("FPSCap", 0)))\n'
     'end\n'
     '-- [port] (user request) Low Render Mode: 3D rendering off and a dashboard (dashboard.lua)\n'
     'L152_1:Toggle({\n'
     '  Name = "Low Render Mode (Dashboard)",\n'
     '  Default = L28_1("LowRender", false),\n'
     '  Callback = function(A0_2)\n'
     '    L27_1("LowRender", A0_2)\n'
     '    getgenv()._YBA_Dashboard.setEnabled(A0_2)\n'
     '  end\n'
     '})\n'
     'if L28_1("LowRender", false) then\n'
     '  getgenv()._YBA_Dashboard.setEnabled(true)\n'
     'end\n'
     '-- [port] (user request) Lag Guard (lagguard.lua)\n'
     'L152_1:Toggle({\n'
     '  Name = "Lag Guard (wait out lag before moving)",\n'
     '  Default = getgenv()._YBA_LagGuard.enabled,\n'
     '  Callback = function(A0_2)\n'
     '    L27_1("LagGuard", A0_2)\n'
     '    getgenv()._YBA_LagGuard.enabled = A0_2 and true or false\n'
     '  end\n'
     '})\n'
     'L152_1:Slider({\n  Name = "Menu Scale",\n', 1),
    # Low Render Mode dashboard (dashboard.lua) data: recent notifications (every farm's status
    # messages go through the notify function), the stand farm's latest skin check and its
    # session stats. Nothing new is sent to the server.
    ('dashboard: notify log',
     'local function L107_1(A0_2, A1_2)\n  local L2_2 = L105_1 or getgenv().RAILhubWindow or _G.RAILhubWindow\n',
     'local function L107_1(A0_2, A1_2)\n'
     '  -- [port] (user request) recent messages for the Low Render Mode dashboard\n'
     '  local dash = getgenv()._YBA_Dashboard\n'
     '  if dash then\n    pcall(dash.log, A0_2, A1_2)\n'
     '    if dash.enabled then\n'
     '      return   -- shown on the dashboard instead of as a pop-up (Low Render Mode)\n'
     '    end\n'
     '  end\n'
     '  local L2_2 = L105_1 or getgenv().RAILhubWindow or _G.RAILhubWindow\n', 1),
    ('dashboard: stand skin',
     '    local L2_2, L3_2 = pcall(function()\n      return L1_2:InvokeServer("ReturnStandSkin", "Stand")\n    end)\n'
     '    return L2_2 and L3_2 or "None"\n  end\n  \n  L149_1.sfCheckShiny = L171_1\n',
     '    local L2_2, L3_2 = pcall(function()\n      return L1_2:InvokeServer("ReturnStandSkin", "Stand")\n    end)\n'
     '    -- [port] (user request) remembered for the Low Render Mode dashboard\n'
     '    local skin = L2_2 and L3_2 or "None"\n'
     '    pcall(function()\n'
     '      getgenv()._YBA_LastSkin = {stand = tostring(L2_1.PlayerStats.Stand.Value), skin = skin}\n'
     '    end)\n'
     '    return skin\n  end\n  \n  L149_1.sfCheckShiny = L171_1\n', 1),
    ('dashboard: stand farm stats', '  startTime = 0\n}\nL149_1.standFarmRespawnConn = nil\n',
     '  startTime = 0\n}\n'
     'getgenv()._YBA_SFStats = L149_1.farmStats   -- [port] for the Low Render Mode dashboard\n'
     'L149_1.standFarmRespawnConn = nil\n', 1),
    # (user request, low-end PC) The window was always created with acrylic blur on (glass parts
    # and a depth-of-field effect rebuilt every frame while it is open), although its "UI
    # Transparency" setting defaults to off and was never applied at load; it now follows it.
    ('perf: window blur follows its setting',
     '    Keybind = Enum.KeyCode.RightControl,\n    AcrylicBlur = true\n  })\n',
     '    Keybind = Enum.KeyCode.RightControl,\n'
     '    AcrylicBlur = L28_1("UIBlur", false) == true   -- [port fix] was always true\n  })\n', 1),
    # Low Render Mode pauses visual-only loops whose output the dashboard covers
    ('low render: hud health', '      if getgenv().HUDHealthEnabled then\n        L0_2 = true\n',
     '      if getgenv().HUDHealthEnabled and not getgenv()._YBA_LowRender then\n        L0_2 = true\n', 1),
    ('low render: watermark', '    if L0_2.Enabled then\n      L6_2.Text = string.format("YBA Thing | %s',
     '    if L0_2.Enabled and not getgenv()._YBA_LowRender then\n      L6_2.Text = string.format("YBA Thing | %s', 1),
    ('low render: player esp',
     '      while L1_2.enabled and L1_2.loopGen == loopGen do\n        refreshPlayerESP()\n        task.wait(0.25)\n',
     '      while L1_2.enabled and L1_2.loopGen == loopGen do\n'
     '        if not getgenv()._YBA_LowRender then\n          refreshPlayerESP()\n        end\n'
     '        task.wait(0.25)\n', 1),
    ('low render: item esp',
     '        if L1_2.enabled and L1_2.loopGen == loopGen then\n          pcall(refreshItemESP)\n',
     '        if L1_2.enabled and L1_2.loopGen == loopGen and not getgenv()._YBA_LowRender then\n'
     '          pcall(refreshItemESP)\n', 1),
    ('low render: weather',
     '        if getgenv().WeatherCfg.Enabled then\n          local L0_3 = getgenv().WeatherCfg.Selected\n',
     '        if getgenv().WeatherCfg.Enabled and not getgenv()._YBA_LowRender then\n'
     '          local L0_3 = getgenv().WeatherCfg.Selected\n', 1),
    # (user request, low-end PC: cull work repeated many times a second; movement, teleports
    # and the bypass are unchanged)
    # Item farm roam: the tween's stop check (auto-sell check + two full item scans) ran twice
    # per frame for the whole roam; it now runs at most every 0.25 s, the farm's own item-wait
    # interval, and keeps its answer in between (a new item ends the roam at most 0.25 s later).
    ('cull: roam stop check',
     '                tweenMoveTo(L9_2, getRandomFarmRoamCFrame(), moveTweenSpeed, function()\n'
     '                  return L6_2() or not normalFarmOn or serverHopFarmEnabled\n                end)\n',
     '                -- [port perf] stop check at most every 0.25 s (it ran twice per frame)\n'
     '                local roamNext, roamHit = 0, false\n'
     '                tweenMoveTo(L9_2, getRandomFarmRoamCFrame(), moveTweenSpeed, function()\n'
     '                  if not roamHit and roamNext <= tick() then\n'
     '                    roamNext = tick() + 0.25\n'
     '                    roamHit = L6_2()\n'
     '                  end\n'
     '                  return roamHit or not normalFarmOn or serverHopFarmEnabled\n                end)\n', 1),
    # PvP auto-block hooks every player's animations and sounds; with BlockBot off, proroc
    # returned after two lookups (TimeStop, RagdollParts) and the hooks first looked up the
    # character's root part. The flag is now checked first (same result: those were pure reads).
    ('cull: blockbot flag mirror', '  function L150_1.setBlockbot(A0_2)\n    L151_1 = A0_2\n  end\n',
     '  function L150_1.setBlockbot(A0_2)\n    L151_1 = A0_2\n'
     '    L150_1.blockbot = A0_2   -- [port perf] read by the animation/sound hooks\n  end\n', 1),
    ('cull: proroc flag first',
     '  function proroc(A0_2, A1_2, A2_2)\n    if L174_1() then\n      return\n    end\n'
     '    if L175_1(game.Players.LocalPlayer.Character) then\n      return\n    end\n'
     '    if not L151_1 then\n      return\n    end\n',
     '  function proroc(A0_2, A1_2, A2_2)\n'
     '    if not L151_1 then   -- [port perf] checked first: the two checks below only read\n      return\n    end\n'
     '    if L174_1() then\n      return\n    end\n'
     '    if L175_1(game.Players.LocalPlayer.Character) then\n      return\n    end\n', 1),
    ('cull: blockbot sound hook',
     '  local function L152_1(A0_2, A1_2)\n    local L2_2 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n    if L2_2 and L151_1 then\n',
     '  local function L152_1(A0_2, A1_2)\n'
     '    if not L150_1.blockbot then   -- [port perf] BlockBot off: nothing to do\n      return\n    end\n'
     '    local L2_2 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n    if L2_2 and L151_1 then\n', 1),
    ('cull: blockbot animation hook 1',
     '    A1_2.AnimationPlayed:Connect(function(A0_3)\n      local L1_3 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n',
     '    A1_2.AnimationPlayed:Connect(function(A0_3)\n'
     '      if not L150_1.blockbot then   -- [port perf] BlockBot off: nothing to do\n        return\n      end\n'
     '      local L1_3 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n', 1),
    ('cull: blockbot animation hook 2',
     '        L2_2.AnimationPlayed:Connect(function(A0_3)\n          local L1_3 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n',
     '        L2_2.AnimationPlayed:Connect(function(A0_3)\n'
     '          if not L150_1.blockbot then   -- [port perf] BlockBot off: nothing to do\n            return\n          end\n'
     '          local L1_3 = A0_2.PrimaryPart or A0_2:FindFirstChild(\"HumanoidRootPart\") or A0_2:FindFirstChild(\"UpperTorso\") or A0_2:FindFirstChild(\"Torso\")\n', 1),
    # every descendant added to any player's character went through up to three IsA calls;
    # Sound and Animator have no subclasses, so a ClassName compare matches IsA exactly
    ('cull: character descendant hook',
     '    A0_2.DescendantAdded:Connect(function(A0_3)\n      if A0_3:IsA("Sound") then\n        L152_1(A0_2, A0_3)\n'
     '      elseif A0_3:IsA("Model") and A0_3.Name == "StandMorph" then\n',
     '    A0_2.DescendantAdded:Connect(function(A0_3)\n'
     '      local cls = A0_3.ClassName   -- [port perf] read once\n'
     '      if cls == "Sound" then\n        L152_1(A0_2, A0_3)\n'
     '      elseif A0_3.Name == "StandMorph" and A0_3:IsA("Model") then\n', 1),
    ('cull: character descendant hook animator',
     '      elseif A0_3:IsA("Animator") then\n        L153_1(A0_2, A0_3)\n      end\n    end)\n',
     '      elseif cls == "Animator" then\n        L153_1(A0_2, A0_3)\n      end\n    end)\n', 1),
    # HUD health text (10 times a second): each GUI level is looked up once instead of
    # re-walking PlayerGui.HUD.Main... from the top for every level
    ('cull: hud health lookups',
     '          local L0_3 = L2_1.PlayerGui:FindFirstChild("HUD") and L2_1.PlayerGui.HUD:FindFirstChild("Main") and L2_1.PlayerGui.HUD.Main:FindFirstChild("Indicators") and L2_1.PlayerGui.HUD.Main.Indicators:FindFirstChild("Bars")\n',
     '          -- [port perf] each level looked up once\n'
     '          local hud = L2_1.PlayerGui:FindFirstChild("HUD")\n'
     '          local hudMain = hud and hud:FindFirstChild("Main")\n'
     '          local hudInd = hudMain and hudMain:FindFirstChild("Indicators")\n'
     '          local L0_3 = hudInd and hudInd:FindFirstChild("Bars")\n', 1),
    # (user request: reads and decisions may change, anything acting on the world stays
    # identical in what and when) Item lookup: the item list is normalised once per scan
    # instead of once per item, the sell blacklist is only consulted for items at their max, and
    # the pickup priority (string work per item) is cached by name; NPC scan: the name test per
    # NPC is cached per target (pure except for Heaven targets, which are not cached).
    ('read: item lookup caches',
     '  function findNearestItem(A0_2)\n'
     '    -- [port perf] backpack counts per item name for this scan (it does not yield)\n'
     '    local countCache = {}\n',
     '  -- [port perf] item priority (Lucky Arrow 1, Lucky Mask 2, candy 3, else 4) by name triple\n'
     '  local itemPriorityCache, itemPriorityCount = {}, 0\n'
     '  \n'
     '  function findNearestItem(A0_2)\n'
     '    -- [port perf] backpack counts per item name for this scan (it does not yield)\n'
     '    local countCache = {}\n'
     '    -- [port perf] the item list normalised once per scan, not once per item on the map\n'
     '    local selNorm, selRaw, selLucky = {}, {}, false\n'
     '    for _, L3_2 in ipairs(A0_2) do\n'
     '      local L4_2 = L144_1 and L144_1(L3_2)\n'
     '      if L4_2 ~= nil then\n'
     '        selNorm[L4_2] = true\n'
     '      end\n'
     '      selRaw[L3_2] = true\n'
     '      if L3_2 == "Lucky Arrow" then\n'
     '        selLucky = true\n'
     '      end\n'
     '    end\n', 1),
    ('read: item list match',
     '          local L21_2 = #A0_2 == 0\n'
     '          if not L21_2 then\n'
     '            local L22_2 = L144_1 and L144_1(L18_2) or L18_2\n'
     '            for L26_2, L27_2 in ipairs(A0_2) do\n'
     '              if L144_1 and L144_1(L27_2) == L22_2 or L27_2 == L18_2 or L27_2 == "Lucky Arrow" and (L18_2 == "Lucky Arrow" or L15_2.Name and L15_2.Name:find("Lucky Arrow")) then\n'
     '                L21_2 = true\n'
     '                break\n'
     '              end\n'
     '            end\n'
     '          end\n',
     '          local L21_2 = #A0_2 == 0\n'
     '          if not L21_2 then\n'
     '            local L22_2 = L144_1 and L144_1(L18_2) or L18_2\n'
     '            L21_2 = selNorm[L22_2] or selRaw[L18_2] or selLucky and (L18_2 == "Lucky Arrow" or L15_2.Name and L15_2.Name:find("Lucky Arrow"))\n'
     '          end\n', 1),
    ('read: sell blacklist order',
     '          if isAutoSellBlacklisted and isAutoSellBlacklisted(L18_2) and L19_2 >= L20_2 then\n',
     '          if L19_2 >= L20_2 and isAutoSellBlacklisted and isAutoSellBlacklisted(L18_2) then\n', 1),
    ('read: item priority',
     '          local L23_2 = 4\n'
     '          local L24_2 = (tostring(L18_2 or "") .. " " .. tostring(L17_2.ObjectText or "") .. " " .. tostring(L15_2.Name or "")):lower()\n'
     '          local L25_2 = L24_2:gsub("[^%a%d]", "")\n'
     '          if L25_2:find("luckyarrow", 1, true) then\n'
     '            L23_2 = 1\n'
     '          elseif L25_2:find("luckystonemask", 1, true) or L25_2:find("luckymask", 1, true) then\n'
     '            L23_2 = 2\n'
     '          elseif L25_2:find("candy", 1, true) then\n'
     '            L23_2 = 3\n'
     '          else\n'
     '            L23_2 = 4\n'
     '          end\n',
     '          local L24_2 = tostring(L18_2 or "") .. " " .. tostring(L17_2.ObjectText or "") .. " " .. tostring(L15_2.Name or "")\n'
     '          local L23_2 = itemPriorityCache[L24_2]\n'
     '          if not L23_2 then\n'
     '            local L25_2 = L24_2:lower():gsub("[^%a%d]", "")\n'
     '            if L25_2:find("luckyarrow", 1, true) then\n'
     '              L23_2 = 1\n'
     '            elseif L25_2:find("luckystonemask", 1, true) or L25_2:find("luckymask", 1, true) then\n'
     '              L23_2 = 2\n'
     '            elseif L25_2:find("candy", 1, true) then\n'
     '              L23_2 = 3\n'
     '            else\n'
     '              L23_2 = 4\n'
     '            end\n'
     '            if 2048 <= itemPriorityCount then\n'
     '              itemPriorityCache, itemPriorityCount = {}, 0\n'
     '            end\n'
     '            itemPriorityCache[L24_2] = L23_2\n'
     '            itemPriorityCount = itemPriorityCount + 1\n'
     '          end\n', 1),
    ('read: npc name match cache',
     '    return false\n'
     '  end\n'
     '  \n'
     '  local function L190_1(A0_2)\n',
     '    return false\n'
     '  end\n'
     '  \n'
     '  -- [port perf] L189_1 per NPC name for the current target: it only reads names, except for a\n'
     '  -- Heaven target (that branch also reads the character\'s position), which is not cached\n'
     '  local npcMatchTarget, npcMatchHeaven, npcMatchCache, npcMatchCount = nil, false, {}, 0\n'
     '  \n'
     '  local function npcNameMatch(A0_2, A1_2)\n'
     '    if A1_2 ~= npcMatchTarget then\n'
     '      npcMatchTarget, npcMatchHeaven, npcMatchCache, npcMatchCount = A1_2, L182_1(A1_2), {}, 0\n'
     '    end\n'
     '    if npcMatchHeaven or A0_2 == nil then\n'
     '      return L189_1(A0_2, A1_2)\n'
     '    end\n'
     '    local L2_2 = npcMatchCache[A0_2]\n'
     '    if L2_2 == nil then\n'
     '      L2_2 = L189_1(A0_2, A1_2) and true or false\n'
     '      if npcMatchCount < 1024 then\n'
     '        npcMatchCache[A0_2] = L2_2\n'
     '        npcMatchCount = npcMatchCount + 1\n'
     '      end\n'
     '    end\n'
     '    return L2_2\n'
     '  end\n'
     '  \n'
     '  local function L190_1(A0_2)\n', 1),
    ('read: npc scan uses the cache',
     'L11_3:IsA("Model") and not L0_1:GetPlayerFromCharacter(L11_3) and L189_1(L11_3.Name, A0_2) and L190_1(L11_3)',
     'L11_3:IsA("Model") and not L0_1:GetPlayerFromCharacter(L11_3) and npcNameMatch(L11_3.Name, A0_2) and L190_1(L11_3)', 1),
    # Dialogue auto-clicker (every 0.15 s) ran the full in-game check before looking for a
    # dialogue; with none showing it clicks nothing, so the dialogue is looked for first. The
    # movement loop (every frame) looked up the root part also with Fly and Speed off.
    ('read: dialogue clicker checks a dialogue first',
     'task.spawn(function()\n'
     '  while true do\n'
     '    task.wait(0.15)\n'
     '    pcall(function()\n'
     '      if not L76_1() then\n'
     '        return\n'
     '      end\n',
     'task.spawn(function()\n'
     '  -- [port perf] no dialogue showing means nothing to click: that is checked first, so the\n'
     '  -- in-game check below (about a dozen lookups) only runs while a dialogue is up\n'
     '  local dialogueNames = {"DialogueGui", "Dialogue", "NPCDialogue"}\n'
     '  while true do\n'
     '    task.wait(0.15)\n'
     '    pcall(function()\n'
     '      local pg = L2_1 and L2_1:FindFirstChild("PlayerGui")\n'
     '      if pg then\n'
     '        local shown = false\n'
     '        for _, nm in ipairs(dialogueNames) do\n'
     '          local g = pg:FindFirstChild(nm)\n'
     '          if g and (g.Enabled or g:FindFirstChild("Frame") and g.Frame.Visible) then\n'
     '            shown = true\n'
     '            break\n'
     '          end\n'
     '        end\n'
     '        if not shown then\n'
     '          return\n'
     '        end\n'
     '      end\n'
     '      if not L76_1() then\n'
     '        return\n'
     '      end\n', 1),
    ('read: movement loop root part only for fly/speed',
     '    local L0_2 = getgenv().PlayerMoveCfg\n'
     '    local L1_2 = L2_1.Character\n'
     '    local L2_2 = L1_2 and L1_2:FindFirstChildOfClass("Humanoid")\n'
     '    local L3_2 = L1_2 and L1_2:FindFirstChild("HumanoidRootPart")\n'
     '    if L2_2 then\n'
     '      L2_2.UseJumpPower = true\n'
     '    end\n'
     '    if not L2_2 or not L3_2 then\n'
     '      return\n'
     '    end\n',
     '    local L0_2 = getgenv().PlayerMoveCfg\n'
     '    local L1_2 = L2_1.Character\n'
     '    local L2_2 = L1_2 and L1_2:FindFirstChildOfClass("Humanoid")\n'
     '    if L2_2 then\n'
     '      L2_2.UseJumpPower = true\n'
     '    end\n'
     '    if not (L0_2.Fly or L0_2.Speed) then   -- [port perf] the root part is only for Fly/Speed\n'
     '      return\n'
     '    end\n'
     '    local L3_2 = L1_2 and L1_2:FindFirstChild("HumanoidRootPart")\n'
     '    if not L2_2 or not L3_2 then\n'
     '      return\n'
     '    end\n', 1),
    # (user request: kicks when the client lags) Lag Guard (lagguard.lua): moves wait for
    # steady frames before they start, a running tween stops at a hitch frame (the farm retries),
    # and kick/error messages are logged with the frame state.
    ('lag guard: tween start',
     '  function tweenMoveTo(A0_2, A1_2, A2_2, A3_2)\n'
     '    if not A0_2 or not A0_2.Parent then\n'
     '      return false\n'
     '    end\n'
     '    stopMoveTween()\n',
     '  function tweenMoveTo(A0_2, A1_2, A2_2, A3_2)\n'
     '    if not A0_2 or not A0_2.Parent then\n'
     '      return false\n'
     '    end\n'
     '    -- [port] (user request) Lag Guard: wait for steady frames before moving\n'
     '    local guard = getgenv()._YBA_LagGuard\n'
     '    if guard and guard.wait() and not A0_2.Parent then\n'
     '      return false\n'
     '    end\n'
     '    stopMoveTween()\n', 1),
    ('lag guard: tween hitch',
     '    L157_1 = L6_1.Stepped:Connect(function()\n'
     '      if not (L5_2 and L5_2.Parent and A0_2) or not A0_2.Parent then\n'
     '        L9_2 = true\n'
     '        return\n'
     '      end\n',
     '    L157_1 = L6_1.Stepped:Connect(function(_, stepDt)\n'
     '      if not (L5_2 and L5_2.Parent and A0_2) or not A0_2.Parent then\n'
     '        L9_2 = true\n'
     '        return\n'
     '      end\n'
     '      -- [port] (user request) Lag Guard: a hitch frame stops the tween before it catches up;\n'
     '      -- the farm retries the move once frames are steady\n'
     '      if guard and guard.enabled and stepDt and stepDt > guard.SPIKE then\n'
     '        pcall(L8_2.Pause, L8_2)\n'
     '        L9_2 = true\n'
     '        return\n'
     '      end\n', 1),
    ('lag guard: moveCharacterTo',
     '  function moveCharacterTo(A0_2, A1_2, A2_2)\n'
     '    if L90_1() then\n',
     '  function moveCharacterTo(A0_2, A1_2, A2_2)\n'
     '    local guard = getgenv()._YBA_LagGuard   -- [port] (user request) Lag Guard\n'
     '    if guard then\n'
     '      guard.wait()\n'
     '    end\n'
     '    if L90_1() then\n', 1),
    ('lag guard: travelToInstant',
     '  function travelToInstant(A0_2)\n'
     '    if L86_1() then\n',
     '  function travelToInstant(A0_2)\n'
     '    local guard = getgenv()._YBA_LagGuard   -- [port] (user request) Lag Guard\n'
     '    if guard then\n'
     '      guard.wait()\n'
     '    end\n'
     '    if L86_1() then\n', 1),
    ('lag guard: npc kill',
     '  local function L194_1(A0_2)\n'
     '    local L1_2 = L2_1.Character\n',
     '  local function L194_1(A0_2)\n'
     '    local guard = getgenv()._YBA_LagGuard   -- [port] (user request) Lag Guard\n'
     '    if guard then\n'
     '      guard.wait()\n'
     '    end\n'
     '    local L1_2 = L2_1.Character\n', 1),
    ('kick log',
     'local function L50_1(A0_2)\n'
     '  if not L48_1 or L49_1 then\n',
     'local function L50_1(A0_2)\n'
     '  -- [port] (user request) kick/error text in YBA/console_log.txt, with the frame state then\n'
     '  pcall(function()\n'
     '    local guard = getgenv()._YBA_LagGuard\n'
     '    print("[Kick/Error] " .. tostring(A0_2) .. " | " .. tostring(game:GetService("GuiService"):GetErrorMessage())\n'
     '      .. (guard and " | Lag Guard: " .. guard.status() or ""))\n'
     '  end)\n'
     '  if not L48_1 or L49_1 then\n', 1),
    # (user request) Timings that assumed 60 FPS: the teleport detector's per-frame distance,
    # tween durations (each frame moves no further than at 60 FPS) and the 5-frame physics
    # nudge. All unchanged at 60 FPS and above; tween/nudge scaling follows the Lag Guard toggle.
    ('low fps: teleport detector frame time',
     '    L6_1.Heartbeat:Connect(function()\n'
     '      local L0_3 = L2_1 or game:GetService("Players") and game:GetService("Players").LocalPlayer\n',
     '    L6_1.Heartbeat:Connect(function(frameDt)\n'
     '      local L0_3 = L2_1 or game:GetService("Players") and game:GetService("Players").LocalPlayer\n', 1),
    ('low fps: teleport detector threshold',
     '          if 18 < L4_3 then\n'
     '            recordTeleport()\n',
     '          -- [port] 18 studs in one frame meant 1080 studs/s at 60 FPS; below 60 FPS the same speed\n'
     '          -- covers more per frame, so the threshold scales with frame time (same at 60 FPS and up)\n'
     '          if math.max(18, 1080 * (frameDt or 0)) < L4_3 then\n'
     '            recordTeleport()\n', 1),
    ('low fps: tween duration',
     '    local L7_2 = math.clamp(L4_2 / L6_2, 0.05, 3)\n',
     '    -- [port] (user request) below 60 FPS the tween takes longer, so each frame moves no further\n'
     '    -- than at 60 FPS (Lag Guard on; unchanged at 60 FPS and up)\n'
     '    local L7_2 = math.clamp(L4_2 / L6_2, 0.05, 3) * (guard and guard.stretch() or 1)\n', 1),
    ('low fps: prestige move guard',
     '    local function L27_2(A0_3, A1_3)\n'
     '      if not A0_3 or not A1_3 then\n'
     '        return\n'
     '      end\n',
     '    local function L27_2(A0_3, A1_3)\n'
     '      if not A0_3 or not A1_3 then\n'
     '        return\n'
     '      end\n'
     '      -- [port] (user request) Lag Guard: wait for steady frames before moving\n'
     '      local guard = getgenv()._YBA_LagGuard\n'
     '      if guard and guard.wait() and not A0_3.Parent then\n'
     '        return\n'
     '      end\n', 1),
    ('low fps: prestige tween duration',
     '        local L3_3 = math.clamp(L2_3 / 425, 0.05, 3)\n',
     '        local L3_3 = math.clamp(L2_3 / 425, 0.05, 3) * (guard and guard.stretch() or 1)   -- [port] low FPS\n', 1),
    ('low fps: physics nudge length',
     '      for L6_2 = 1, 5 do\n'
     '        L6_1.Heartbeat:Wait()\n'
     '      end\n',
     '      -- [port] 5 frames was about 83 ms at 60 FPS; below that it is kept near 83 ms (Lag Guard on)\n'
     '      local guard = getgenv()._YBA_LagGuard\n'
     '      local frames = guard and math.max(1, math.floor(5 / guard.stretch() + 0.5)) or 5\n'
     '      for L6_2 = 1, frames do\n'
     '        L6_1.Heartbeat:Wait()\n'
     '      end\n', 1),
    # (user request) Server hop off by default; three "is server hop on" helpers ended in
    # "or true" and always said yes (the stand farm's own item search then gave up after the hop
    # delay instead of 20 s). Settings and boot log per account (UserId), seeded from the shared
    # settings file the first time.
    ('hop off: item farm default',
     '  serverHopFarmEnabled = L28_1("SHF", true)\n',
     '  serverHopFarmEnabled = L28_1("SHF", false)   -- [port] (user request) off by default\n', 1),
    ('hop off: toggle default',
     '    Default = L28_1("SHF", true),\n',
     '    Default = L28_1("SHF", false),\n', 1),
    ('hop off: wrappers report the setting',
     '    return L139_1.serverHopFarmEnabled and L139_1.serverHopFarmEnabled() or L28_1 and L28_1("SHF", true) or true\n',
     '    -- [port fix] was "... or true", so it always said server hop was on\n'
     '    if L139_1.serverHopFarmEnabled then\n'
     '      return L139_1.serverHopFarmEnabled() == true\n'
     '    end\n'
     '    return L28_1 and L28_1("SHF", false) == true\n', 3),
    ('hop off: auto requiem',
     '    local L3_2 = L149_1.serverHopNoItems ~= false and L28_1 and L28_1("SHF", true) ~= false\n',
     '    local L3_2 = L149_1.serverHopNoItems ~= false and L28_1 and L28_1("SHF", false) ~= false\n', 1),
    ('per account: settings file',
     'local L18_1 = "YBA"\n'
     'local L19_1 = L18_1 .. "/custom_flags.json"\n',
     'local L18_1 = "YBA"\n'
     'local L19_1 = L18_1 .. "/custom_flags.json"\n'
     '-- [port] (user request) settings per account: accounts executing on one PC shared this file and\n'
     '-- overwrote each other\'s settings (each saves its whole settings table). An account\'s own file\n'
     '-- starts as a copy of the shared one, so existing settings carry over.\n'
     'do\n'
     '  local uid = L2_1 and L2_1.UserId\n'
     '  if uid and uid ~= 0 then\n'
     '    local own = L18_1 .. "/custom_flags_" .. tostring(uid) .. ".json"\n'
     '    pcall(function()\n'
     '      if type(isfile) == "function" and type(readfile) == "function" and type(writefile) == "function"\n'
     '        and not isfile(own) and isfile(L19_1) then\n'
     '        writefile(own, readfile(L19_1))\n'
     '      end\n'
     '    end)\n'
     '    L19_1 = own\n'
     '  end\n'
     'end\n', 1),
    ('per account: boot log',
     'local L16_1 = "YBA/boot_log.txt"\n',
     'local L16_1 = "YBA/boot_log" .. (L2_1 and L2_1.UserId ~= 0 and "_" .. tostring(L2_1.UserId) or "") .. ".txt"   -- [port] per account\n', 1),
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
     '    local coordPause = function()\n      L150_1()\n      L155_1()\n    end\n'
     '    -- waiting, it takes a turn only with an item to collect (or the server hop farm on);\n'
     '    -- without one it roamed to a random spot and handed straight back\n'
     '    local coordReady = function()\n'
     '      return serverHopFarmEnabled or (findLuckyArrow() or findNearestItem(selectedFarmItems)) ~= nil\n'
     '    end\n'),
    ('item farm loop',
     '        while normalFarmOn do\n          if L126_1 and L126_1() then\n            normalFarmOn = false\n'
     '            break\n          end\n          if L86_1() then\n',
     f'          if not {CP}("ItemFarm", coordOn, false, coordPause, noteItemFarmProgress, coordReady) then\n'
     '            break\n          end\n'),
    ('item farm roam loop',
     '          while normalFarmOn do\n            local L7_2 = false\n            if serverHopFarmEnabled then\n',
     f'            if not {CP}("ItemFarm", coordOn, true, coordPause, noteItemFarmProgress, coordReady) then\n'
     '              break\n            end\n'),
    # stand farms (normal, rib, shiny): one participant. The stand farm is idle and not ready
    # while it waits for the item farm to collect its arrows and Rokakaka (sfCoordReady).
    ('stand farm loop',
     '    while L149_1.standFarmRunning do\n      local L2_2 = false\n      while true do\n        L1_2 = L1_2 + 1\n',
     '      local sfReady = sfCoordReady()\n'
     f'      if not {CP}("StandFarm", function()\n'
     '        return L149_1.standFarmRunning or L149_1.ribFarmRunning or L149_1.shinyFarmRunning\n'
     '      end, not sfReady, nil, nil, sfCoordReady) then\n        break\n      end\n'),
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


def main(src, loggerf, *rest):
    *modules, out = rest
    with open(src, 'r', encoding='latin-1') as f:
        text = f.read()
    for name, old, new, count in EDITS:
        found = text.count(old)
        assert found == count, f'{name}: expected {count} matches, found {found}'
        text = text.replace(old, new)
    text = strip_reload(text)
    parts = []
    for path in modules:
        with open(path, 'r', encoding='ascii') as f:
            parts.append(f.read().rstrip('\n'))
    text = add_coordinator(text, '\n\n'.join(parts) + '\n')
    with open(loggerf, 'r', encoding='ascii') as f:
        logger = f.read().rstrip('\n')
    result = logger + '\n\n' + text
    with open(out, 'w', encoding='latin-1') as f:
        f.write(result)
    print(f'Written {out} ({result.count(chr(10)) + 1} lines)')


if __name__ == '__main__':
    main(*sys.argv[1:])
