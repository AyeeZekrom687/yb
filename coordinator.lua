-- [port] Coordinator (user request): features that drive the character (item farm, stand
-- farms, quest/NPC farm, auto prestige) take turns instead of fighting over it. Each one calls
-- checkpoint() at safe points (the start of a loop iteration or of an action). With one
-- feature running, checkpoint() returns at once, so single-feature behaviour is the original's.
-- Turn changes are printed as "[Coordinator] ..." (mirrored to YBA/console_log.txt).
do
  local C = {
    enabled = L28_1("COORD", true) ~= false,
    slice = tonumber(L28_1("COORD_SLICE", 60)) or 60,
    holder = nil,         -- feature that may drive the character
    sliceStart = 0,
    waiting = {},         -- feature -> tick() when it started waiting
    active = {},          -- feature -> function: is the feature still switched on
    seen = {},            -- feature -> tick() of its latest checkpoint
    STALE = 120,          -- a holder silent this long while on is treated as stuck
    -- the quest farm and Auto Prestige's storyline share the game's single quest slot
    yieldsTo = {QuestFarm = "AutoPrestige"},
    names = {
      ItemFarm = "Item Farm",
      StandFarm = "Stand Farm",
      QuestFarm = "Quest/NPC Farm",
      AutoPrestige = "Auto Prestige"
    }
  }
  getgenv()._YBA_Coord = C

  local function running(name)
    local f = C.active[name]
    if not f then
      return false
    end
    local ok, on = pcall(f)
    return ok and on and true or false
  end

  local function blocked(name)
    local other = C.yieldsTo[name]
    return other ~= nil and running(other)
  end

  -- the holder no longer needs the character: switched off, or stuck outside any checkpoint
  local function vacant(holder)
    return holder == nil or not running(holder) or tick() - (C.seen[holder] or 0) > C.STALE
  end

  -- the feature that has waited longest and may run now, other than except
  local function nextWaiter(except)
    local best, bestT
    for name, t in pairs(C.waiting) do
      if name ~= except and running(name) and not blocked(name) and (bestT == nil or t < bestT) then
        best, bestT = name, t
      end
    end
    return best
  end

  local function grant(name)
    if C.holder ~= name then
      print("[Coordinator] " .. tostring(C.names[C.holder] or "idle") .. " -> " .. tostring(C.names[name] or name))
    end
    C.holder = name
    C.sliceStart = tick()
    C.seen[name] = tick()
    C.waiting[name] = nil
  end

  -- name: feature id; isOn(): whether the feature is still switched on; idle: it has nothing to
  -- do right now (hand over at once if another feature waits); onPause(): stops the feature's own
  -- movement before another takes over; onWait(): called every 0.25 s while it waits.
  -- Returns false when the feature was switched off while waiting (the caller leaves its loop).
  function C.checkpoint(name, isOn, idle, onPause, onWait)
    if not C.enabled then
      return true
    end
    C.active[name] = isOn
    C.seen[name] = tick()
    local holder = C.holder
    if holder == name or vacant(holder) then
      if not blocked(name) then
        if holder ~= name then
          grant(name)
        end
        local other = nextWaiter(name)
        if other == nil or not idle and tick() - C.sliceStart < C.slice then
          return true
        end
        grant(other)
      elseif holder == name then
        -- blocked while holding (the feature it yields to has started): let others run
        C.holder = nil
        local other = nextWaiter(name)
        if other then
          grant(other)
        end
      end
    end
    if onPause then
      pcall(onPause)
    end
    C.waiting[name] = C.waiting[name] or tick()
    while true do
      task.wait(0.25)
      if not C.enabled then
        C.waiting[name] = nil
        return true
      end
      if not running(name) then
        C.waiting[name] = nil
        return false
      end
      if C.holder == name then
        C.seen[name] = tick()
        return true
      end
      if vacant(C.holder) and nextWaiter(nil) == name then
        grant(name)
        return true
      end
      if onWait then
        pcall(onWait)
      end
    end
  end

  -- whether name may move the character now (for event handlers such as respawn teleports)
  function C.allows(name)
    return not C.enabled or C.holder == name or vacant(C.holder)
  end

  function C.setEnabled(on)
    C.enabled = on and true or false
    C.holder = nil
  end
end
