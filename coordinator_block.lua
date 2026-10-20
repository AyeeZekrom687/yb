                            -- ═══════════════════════════════════════════════════════
                            -- YBA Farm Coordinator
                            -- Orchestrates all farming systems so they work together
                            -- ═══════════════════════════════════════════════════════
                            do
                                local _coordLog = _r110
                                local _coord = {}
                                _coord.enabled = false
                                _coord.thread = nil
                                _coord.currentPhase = nil
                                _coord.gen = 0

                                local SETTLE_TIME = 3

                                local coordSettings = {
                                    itemFarmTime = 120,
                                    standFarmTime = 120,
                                    questFarmTime = 90,
                                    npcFarmTime = 90,
                                    prestigeFirst = true,
                                }

                                local coordFlags = {
                                    prestige = false,
                                    itemFarm = false,
                                    standFarm = false,
                                    questFarm = false,
                                    npcFarm = false,
                                }

                                local function coordWait(secs, gen)
                                    local deadline = tick() + secs
                                    while tick() < deadline and _coord.enabled and _coord.gen == gen do
                                        task.wait(0.5)
                                    end
                                    return _coord.enabled and _coord.gen == gen
                                end

                                local function stopPhase(name)
                                    if not name then return end
                                    local ok, err = pcall(function()
                                        if name == "ItemFarm" then
                                            if _G.YBAItemFarm and _G.YBAItemFarm.stopFarming then
                                                _G.YBAItemFarm.stopFarming()
                                            end
                                        elseif name == "StandFarm" then
                                            if _G.YBAItemFarm and _G.YBAItemFarm.stopStandFarm then
                                                _G.YBAItemFarm.stopStandFarm()
                                            end
                                        elseif name == "QuestFarm" then
                                            local qf = getgenv()._YBA_CoordQuest
                                            if qf and qf.stop then qf.stop() end
                                        elseif name == "NpcFarm" then
                                            local nf = getgenv()._YBA_CoordNpc
                                            if nf and nf.stop then nf.stop() end
                                        elseif name == "AutoPrestige" then
                                            local tp = getgenv().YBA_ToggleAutoPrestige or (_G and _G.toggleAutoPrestige)
                                            if tp then tp(false) end
                                        end
                                    end)
                                    if not ok and _coordLog then
                                        pcall(_coordLog, "Coordinator", "Error stopping " .. tostring(name) .. ": " .. tostring(err))
                                    end
                                end

                                local function startPhase(name)
                                    local ok, err = pcall(function()
                                        if name == "ItemFarm" then
                                            if _G.YBAItemFarm and _G.YBAItemFarm.startFarming then
                                                _G.YBAItemFarm.startFarming()
                                            end
                                        elseif name == "StandFarm" then
                                            if _G.YBAItemFarm and _G.YBAItemFarm.startStandFarm then
                                                _G.YBAItemFarm.startStandFarm()
                                            end
                                        elseif name == "QuestFarm" then
                                            local qf = getgenv()._YBA_CoordQuest
                                            if qf and qf.start then qf.start() end
                                        elseif name == "NpcFarm" then
                                            local nf = getgenv()._YBA_CoordNpc
                                            if nf and nf.start then nf.start() end
                                        elseif name == "AutoPrestige" then
                                            local tp = getgenv().YBA_ToggleAutoPrestige or (_G and _G.toggleAutoPrestige)
                                            if tp then tp(true) end
                                        end
                                    end)
                                    if not ok and _coordLog then
                                        pcall(_coordLog, "Coordinator", "Error starting " .. tostring(name) .. ": " .. tostring(err))
                                    end
                                end

                                local function isPhaseRunning(name)
                                    local ok, result = pcall(function()
                                        if name == "ItemFarm" then
                                            return _G.YBAItemFarm and _G.YBAItemFarm.isItemFarmRunning and _G.YBAItemFarm.isItemFarmRunning()
                                        elseif name == "StandFarm" then
                                            return _G.YBAItemFarm and _G.YBAItemFarm.isStandFarmRunning and _G.YBAItemFarm.isStandFarmRunning()
                                        elseif name == "QuestFarm" then
                                            local qf = getgenv()._YBA_CoordQuest
                                            return qf and qf.isRunning and qf.isRunning()
                                        elseif name == "NpcFarm" then
                                            local nf = getgenv()._YBA_CoordNpc
                                            return nf and nf.isRunning and nf.isRunning()
                                        elseif name == "AutoPrestige" then
                                            local pp = getgenv()._YBA_CoordPrestige
                                            return pp and pp.isRunning and pp.isRunning()
                                        end
                                        return false
                                    end)
                                    return ok and result
                                end

                                local function coordLoop()
                                    local gen = _coord.gen
                                    if _coordLog then
                                        pcall(_coordLog, "Coordinator", "Farm Coordinator started.")
                                    end

                                    while _coord.enabled and _coord.gen == gen do
                                        if coordFlags.prestige and coordSettings.prestigeFirst then
                                            _coord.currentPhase = "AutoPrestige"
                                            if _coordLog then
                                                pcall(_coordLog, "Coordinator", "Phase: Auto Prestige (runs to completion)")
                                            end
                                            startPhase("AutoPrestige")
                                            while _coord.enabled and _coord.gen == gen do
                                                task.wait(2)
                                                if not isPhaseRunning("AutoPrestige") then break end
                                            end
                                            _coord.currentPhase = nil
                                            if not (_coord.enabled and _coord.gen == gen) then break end
                                            if not coordWait(SETTLE_TIME, gen) then break end
                                        end

                                        local phases = {}
                                        if coordFlags.itemFarm then
                                            table.insert(phases, {name = "ItemFarm", time = coordSettings.itemFarmTime})
                                        end
                                        if coordFlags.standFarm then
                                            table.insert(phases, {name = "StandFarm", time = coordSettings.standFarmTime})
                                        end
                                        if coordFlags.questFarm then
                                            table.insert(phases, {name = "QuestFarm", time = coordSettings.questFarmTime})
                                        end
                                        if coordFlags.npcFarm then
                                            table.insert(phases, {name = "NpcFarm", time = coordSettings.npcFarmTime})
                                        end

                                        if #phases == 0 then
                                            task.wait(1)
                                        else
                                            for _, phase in ipairs(phases) do
                                                if not (_coord.enabled and _coord.gen == gen) then break end
                                                _coord.currentPhase = phase.name
                                                if _coordLog then
                                                    pcall(_coordLog, "Coordinator", "Phase: " .. phase.name .. " (" .. tostring(phase.time) .. "s)")
                                                end
                                                startPhase(phase.name)
                                                if not coordWait(phase.time, gen) then break end
                                                stopPhase(phase.name)
                                                _coord.currentPhase = nil
                                                if not coordWait(SETTLE_TIME, gen) then break end
                                            end
                                        end
                                    end

                                    _coord.currentPhase = nil
                                    if _coordLog then
                                        pcall(_coordLog, "Coordinator", "Farm Coordinator stopped.")
                                    end
                                end

                                function _coord.start()
                                    if _coord.enabled then return end
                                    _coord.enabled = true
                                    _coord.gen = _coord.gen + 1
                                    _coord.thread = task.spawn(coordLoop)
                                end

                                function _coord.stop()
                                    _coord.enabled = false
                                    _coord.gen = _coord.gen + 1
                                    local phase = _coord.currentPhase
                                    if phase then
                                        stopPhase(phase)
                                        _coord.currentPhase = nil
                                    end
                                end

                                function _coord.setFlag(name, val)
                                    if coordFlags[name] ~= nil then
                                        coordFlags[name] = (val == true)
                                    end
                                end

                                function _coord.getFlag(name)
                                    return coordFlags[name]
                                end

                                function _coord.setTime(name, secs)
                                    local key = name .. "Time"
                                    if coordSettings[key] ~= nil then
                                        coordSettings[key] = math.max(10, math.min(600, tonumber(secs) or 120))
                                    end
                                end

                                function _coord.getTime(name)
                                    local key = name .. "Time"
                                    return coordSettings[key]
                                end

                                function _coord.getStatus()
                                    return {
                                        enabled = _coord.enabled,
                                        currentPhase = _coord.currentPhase,
                                        flags = coordFlags,
                                        config = coordSettings,
                                    }
                                end

                                getgenv()._YBA_Coordinator = _coord

                                pcall(function()
                                    local tabGroup = getgenv()._YBA_TabGroup
                                    if not tabGroup then return end

                                    local coordTab = tabGroup:Tab({Name = "Coordinator", Image = "rbxassetid://7734053495"})

                                    local mainSection = coordTab:Section({Side = "Left", Name = "Farm Coordinator"})
                                    mainSection:Toggle({
                                        Name = "Enable Coordinator",
                                        Default = false,
                                        Callback = function(val)
                                            if val then
                                                _coord.start()
                                            else
                                                _coord.stop()
                                            end
                                        end,
                                    })

                                    local systemsSection = coordTab:Section({Side = "Left", Name = "Systems to Coordinate"})
                                    systemsSection:Toggle({
                                        Name = "Include Item Farm",
                                        Default = false,
                                        Callback = function(val) coordFlags.itemFarm = (val == true) end,
                                    })
                                    systemsSection:Toggle({
                                        Name = "Include Stand Farm",
                                        Default = false,
                                        Callback = function(val) coordFlags.standFarm = (val == true) end,
                                    })
                                    systemsSection:Toggle({
                                        Name = "Include Quest Farm",
                                        Default = false,
                                        Callback = function(val) coordFlags.questFarm = (val == true) end,
                                    })
                                    systemsSection:Toggle({
                                        Name = "Include NPC Farm",
                                        Default = false,
                                        Callback = function(val) coordFlags.npcFarm = (val == true) end,
                                    })
                                    systemsSection:Toggle({
                                        Name = "Include Auto Prestige",
                                        Default = false,
                                        Callback = function(val) coordFlags.prestige = (val == true) end,
                                    })

                                    local timingSection = coordTab:Section({Side = "Right", Name = "Phase Durations"})
                                    local timeOptions = {"30", "60", "90", "120", "180", "240", "300"}
                                    timingSection:Dropdown({
                                        Name = "Item Farm Phase (sec)",
                                        Options = timeOptions,
                                        Default = "120",
                                        Callback = function(val) coordSettings.itemFarmTime = tonumber(val) or 120 end,
                                    })
                                    timingSection:Dropdown({
                                        Name = "Stand Farm Phase (sec)",
                                        Options = timeOptions,
                                        Default = "120",
                                        Callback = function(val) coordSettings.standFarmTime = tonumber(val) or 120 end,
                                    })
                                    timingSection:Dropdown({
                                        Name = "Quest Farm Phase (sec)",
                                        Options = timeOptions,
                                        Default = "90",
                                        Callback = function(val) coordSettings.questFarmTime = tonumber(val) or 90 end,
                                    })
                                    timingSection:Dropdown({
                                        Name = "NPC Farm Phase (sec)",
                                        Options = timeOptions,
                                        Default = "90",
                                        Callback = function(val) coordSettings.npcFarmTime = tonumber(val) or 90 end,
                                    })

                                    local settingsSection = coordTab:Section({Side = "Right", Name = "Coordinator Settings"})
                                    settingsSection:Toggle({
                                        Name = "Prestige First (Blocking)",
                                        Default = true,
                                        Callback = function(val) coordSettings.prestigeFirst = (val == true) end,
                                    })
                                end)
                            end
