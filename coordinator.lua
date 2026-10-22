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
    ready = {},           -- feature -> function: it has something to do now (absent: always)
    STALE = 120,          -- a holder silent this long while on is treated as stuck
    -- the quest farm and Auto Prestige's storyline share the game's single quest slot
    yieldsTo = {QuestFarm = "AutoPrestige"},
    -- while both run, the stand farm gets its arrows and Rokakaka from the item farm
    feeds = {StandFarm = "ItemFarm"},
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

  local function ownReady(name)
    local f = C.ready[name]
    if not f then
      return true
    end
    local ok, r = pcall(f)
    return not ok or (r and true or false)   -- a failing check never starves a feature
  end

  -- a feature has something to do: by its own check, or a running feature it feeds waits on it
  local function ready(name)
    if ownReady(name) then
      return true
    end
    for other, source in pairs(C.feeds) do
      if source == name and running(other) and not ownReady(other) then
        return true
      end
    end
    return false
  end

  -- the feature that has waited longest and may run now, other than except; only ready ones
  -- unless anyState
  local function nextWaiter(except, anyState)
    local best, bestT
    for name, t in pairs(C.waiting) do
      if name ~= except and running(name) and not blocked(name) and (bestT == nil or t < bestT)
        and (anyState or ready(name)) then
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
  -- movement before another takes over; onWait(): called every 0.25 s while it waits;
  -- readyFn(): while it waits, whether it has something to do (a turn is only handed to a ready
  -- feature; when none is, the longest waiting one takes a vacant lease).
  -- Returns false when the feature is off or was switched off while waiting (the caller leaves
  -- its loop). An old session still looping after its toggle went off (Auto Prestige's storyline
  -- loop never checked) stops here instead of running forever.
  function C.checkpoint(name, isOn, idle, onPause, onWait, readyFn)
    local ok, on = pcall(isOn)
    if not (ok and on) then
      return false
    end
    -- Lag Guard (lagguard.lua): no new action starts while the client lags
    local guard = getgenv()._YBA_LagGuard
    if guard and guard.wait() then
      ok, on = pcall(isOn)
      if not (ok and on) then
        return false
      end
    end
    if not C.enabled then
      return true
    end
    C.active[name] = isOn
    C.ready[name] = readyFn
    C.seen[name] = tick()
    local holder = C.holder
    if holder == name or vacant(holder) then
      if not blocked(name) then
        if holder ~= name then
          grant(name)
        end
        if not idle and tick() - C.sliceStart < C.slice then
          return true
        end
        local other = nextWaiter(name)
        if other == nil then
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
      if vacant(C.holder) and (nextWaiter(nil) or nextWaiter(nil, true)) == name then
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
