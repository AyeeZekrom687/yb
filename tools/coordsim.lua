-- Standalone simulation of coordinator.lua: virtual clock, cooperative tasks.
-- Features: ItemFarm (idle when no items; ready = items on map or stand farm starving via feeds)
-- and StandFarm (ready = has >= 1 arrow and >= 1 roka; idle = not ready).
local now = 0
local tasks = {}
function tick() return now end
local G = {}
function getgenv() return G end
task = {}
function task.wait(s)
  coroutine.yield(s or 0.03)
end
local log = {}
local function out(s) log[#log + 1] = string.format("%6.1f %s", now, s) end
print = function(s) out(s) end
local function L28_1(k, d) return d end
local src = io.open(arg[1]):read("*a")
assert(loadstring("local L28_1 = ...\n" .. src))(L28_1)
local C = G._YBA_Coord

local function spawn(fn) tasks[#tasks + 1] = { co = coroutine.create(fn), wake = now } end
local function run(untilT)
  while now < untilT do
    local best
    for _, t in ipairs(tasks) do
      if coroutine.status(t.co) ~= "dead" and (not best or t.wake < best.wake) then best = t end
    end
    if not best then break end
    now = math.max(now, best.wake)
    local ok, d = coroutine.resume(best.co)
    assert(ok, d)
    best.wake = now + (d or 0)
  end
end

-- world state
local W = { items = 0, arrows = 0, rokas = 0, itemOn = true, standOn = true }
local moves = { item_roam = 0, item_collect = 0, stand_roll = 0, stand_selfcollect = 0 }

-- item farm: per-item loop (busy) then roam loop (idle), as in normalFarm
spawn(function()
  local coordOn = function() return W.itemOn end
  local ready = function() return W.items > 0 end
  while W.itemOn do
    while W.itemOn do
      if not C.checkpoint("ItemFarm", coordOn, false, nil, nil, ready) then break end
      if W.items == 0 then break end
      W.items = W.items - 1
      moves.item_collect = moves.item_collect + 1
      if W.nextDrop then W[W.nextDrop] = W[W.nextDrop] + 1 end
      task.wait(2)
    end
    while W.itemOn do
      if not C.checkpoint("ItemFarm", coordOn, true, nil, nil, ready) then break end
      if W.items > 0 then break end
      moves.item_roam = moves.item_roam + 1
      out("item farm roams")
      task.wait(1)
    end
  end
end)

-- stand farm loop
spawn(function()
  local isOn = function() return W.standOn end
  local sfReady = function() return not W.itemOn or (W.arrows > 0 and W.rokas > 0) end
  while W.standOn do
    local r = sfReady()
    if not C.checkpoint("StandFarm", isOn, not r, nil, nil, sfReady) then break end
    if not sfReady() then
      task.wait(0.5)
    elseif W.arrows > 0 and W.rokas > 0 then
      W.arrows = W.arrows - 1
      W.rokas = W.rokas - 1
      moves.stand_roll = moves.stand_roll + 1
      task.wait(3)
    else
      moves.stand_selfcollect = moves.stand_selfcollect + 1
      task.wait(3)
    end
  end
end)

local scenario = arg[2]
if scenario == "noitems_hasitems" then
  W.items, W.arrows, W.rokas = 0, 100, 100
  run(300)
elseif scenario == "items_lacking" then
  W.items, W.arrows, W.rokas = 6, 0, 0
  W.nextDrop = nil
  -- items alternate: collecting gives roka then arrow
  local drops = { "rokas", "arrows", "rokas", "arrows", "rokas", "arrows" }
  local i = 0
  local orig = moves
  spawn(function()
    while true do
      task.wait(0.1)
      local collected = moves.item_collect
      while i < collected do i = i + 1; local k = drops[i]; if k then W[k] = W[k] + 1 end end
    end
  end)
  run(300)
elseif scenario == "noitems_lacking" then
  W.items, W.arrows, W.rokas = 0, 0, 0
  run(120)
  out("-- items spawn: 1 roka, 1 arrow")
  W.items = 2
  local drops = { "rokas", "arrows" }
  local i = 0
  spawn(function()
    while true do
      task.wait(0.1)
      while i < moves.item_collect do i = i + 1; local k = drops[i]; if k then W[k] = W[k] + 1 end end
    end
  end)
  run(300)
elseif scenario == "both_ready" then
  W.items, W.arrows, W.rokas = 1000, 1000, 1000
  run(300)
elseif scenario == "stand_off" then
  W.items, W.arrows, W.rokas = 0, 100, 100
  run(100)
  out("-- stand farm switched off")
  W.standOn = false
  run(110)
elseif scenario == "item_off" then
  W.items, W.arrows, W.rokas = 0, 0, 0
  run(50)
  out("-- item farm switched off")
  W.itemOn = false
  run(70)
elseif scenario == "item_alone" then
  W.standOn = false
  W.items = 0
  run(30)
end
for _, l in ipairs(log) do
  if not l:find("item farm roams") or scenario == "item_alone" or scenario == "noitems_hasitems" or scenario == "stand_off" then io.write(l, "\n") end
end
io.write(string.format("roam=%d collect=%d rolls=%d selfcollect=%d holder=%s\n", moves.item_roam,
  moves.item_collect, moves.stand_roll, moves.stand_selfcollect, tostring(C.holder)))
