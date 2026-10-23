-- [port] Lag Guard (user request: kicks when the client lags). A hitch makes movement catch up
-- in one frame (a tween at 425 studs/s covers 425 studs after a 1 s freeze) and bunches actions
-- together. While frames are unsteady, moves wait before they start (coordinator checkpoints,
-- tweenMoveTo, moveCharacterTo, travelToInstant, the NPC kill) and a running tween is stopped at
-- the first hitch frame (the farm retries the move). With steady frames nothing waits.
do
  local G = {
    enabled = L28_1("LagGuard", true) ~= false,
    SPIKE = 0.25,      -- a frame longer than this (seconds) is a hitch
    SETTLE = 1.5,      -- seconds of steady frames needed after a hitch
    SLOW = 0.1,        -- average frame time above this (under 10 FPS) counts as lagging
    MAX_WAIT = 10,     -- a move waits at most this long, then goes ahead
    avg = 1 / 60,      -- moving average of frame time
    lastSpike = 0,     -- tick() of the latest hitch
    lastSpikeLen = 0,
    waiting = 0,       -- moves currently held
    lastLog = 0
  }
  getgenv()._YBA_LagGuard = G

  L6_1.Heartbeat:Connect(function(dt)
    G.avg = G.avg + (dt - G.avg) * 0.1
    if dt > G.SPIKE then
      G.lastSpike = tick()
      G.lastSpikeLen = dt
    end
  end)

  function G.unstable()
    return G.enabled and (tick() - G.lastSpike < G.SETTLE or G.avg > G.SLOW)
  end

  -- holds the caller while the client lags (at most MAX_WAIT); returns at once when steady.
  -- Returns true when it held (the caller re-checks anything that may have changed meanwhile).
  function G.wait()
    if not G.unstable() then
      return false
    end
    if tick() - G.lastLog > 5 then
      G.lastLog = tick()
      print(string.format("[Lag Guard] holding moves: last hitch %.2f s, %.1fs ago, average frame %.3f s",
        G.lastSpikeLen, tick() - G.lastSpike, G.avg))
    end
    G.waiting = G.waiting + 1
    local start = tick()
    while G.unstable() and tick() - start < G.MAX_WAIT do
      task.wait(0.1)
    end
    G.waiting = G.waiting - 1
    return true
  end

  -- the script's movement timings assume 60 FPS: how much longer a move must take at the
  -- current frame rate so each frame moves no further than at 60 FPS (1 at 60 FPS or above)
  function G.stretch()
    if not G.enabled then
      return 1
    end
    return math.clamp(60 * G.avg, 1, 6)
  end

  function G.status()
    if G.lastSpike == 0 then
      return string.format("no hitch yet, average frame %.3f s", G.avg)
    end
    return string.format("last hitch %.2f s, %ds ago, average frame %.3f s",
      G.lastSpikeLen, math.floor(tick() - G.lastSpike), G.avg)
  end
end
