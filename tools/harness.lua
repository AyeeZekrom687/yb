-- Mock Roblox/executor harness for tracing the YBA script under Lua 5.1.
-- Usage: lua5.1 harness.lua SCRIPT [MAX_MAIN_STEPS]
local SCRIPT = assert(arg[1], "script path required")
local BUDGET = tonumber(arg[2]) or 200000000   -- instructions per thread before abort
local HOOK_EVERY = 100000

local real_print = print
local LOG_COUNTS, LOG_AGG = {}, false   -- LOG_AGG: print each distinct line once, count repeats
local function log(tag, msg)
    local s = string.format("[%s] %s", tag, tostring(msg))
    if LOG_AGG then
        local c = (LOG_COUNTS[s] or 0) + 1
        LOG_COUNTS[s] = c
        if c > 1 then return end
    end
    real_print(s)
end

------------------------------------------------------------------------
-- Clock
------------------------------------------------------------------------
local now = 1000
local function clock() return now end

------------------------------------------------------------------------
-- Instruction budget (Lua 5.1 hooks are per-thread)
------------------------------------------------------------------------
local function install_budget(co, label)
    local used = 0
    local function hook()
        used = used + HOOK_EVERY
        if used > BUDGET then
            local info = debug.getinfo(2, "Sl")
            used = 0
            error(string.format("BUDGET exceeded in %s at line %s", label, info and info.currentline or "?"), 2)
        end
    end
    if co then debug.sethook(co, hook, "", HOOK_EVERY) else debug.sethook(hook, "", HOOK_EVERY) end
end

------------------------------------------------------------------------
-- Scheduler
------------------------------------------------------------------------
local tasks = {}          -- { co = thread, wake = time, label = string }
local main_co = nil       -- nil means the main Lua thread
local task_seq = 0

local TASK_ERRS = {}
local function resume_task(t, ...)
    local ok, err = coroutine.resume(t.co, ...)
    if not ok then
        local msg = tostring(err)
        TASK_ERRS[msg] = (TASK_ERRS[msg] or 0) + 1
        if TASK_ERRS[msg] == 1 then
            log("TASK ERR", t.label .. ": " .. msg)
            real_print(debug.traceback(t.co))
        end
        return false
    end
    if coroutine.status(t.co) == "dead" then return false end
    t.wake = now + (tonumber(err) or 0)
    return true
end

local function new_task(f, label, ...)
    task_seq = task_seq + 1
    local info = debug.getinfo(f, "S")
    local t = { co = coroutine.create(f), wake = now,
                label = (label or "task") .. "#" .. task_seq .. "@" .. tostring(info and info.linedefined) }
    install_budget(t.co, t.label)
    return t
end

local function spawn_now(f, ...)
    if type(f) ~= "function" then return end
    local t = new_task(f, "spawn")
    if resume_task(t, ...) then tasks[#tasks + 1] = t end
    return t.co
end

local function schedule(delay, f, ...)
    if type(f) ~= "function" then return end
    local t = new_task(f, "delay")
    t.wake = now + (delay or 0)
    t.args = { ... }
    t.fresh = true
    tasks[#tasks + 1] = t
    return t.co
end

-- Run every task whose wake time has passed, once. Returns number resumed.
local function run_due()
    local n = 0
    local i = 1
    while i <= #tasks do
        local t = tasks[i]
        if t.wake <= now then
            local alive
            if t.fresh then
                t.fresh = nil
                alive = resume_task(t, unpack(t.args))
            else
                alive = resume_task(t)
            end
            n = n + 1
            if alive then i = i + 1 else table.remove(tasks, i) end
        else
            i = i + 1
        end
    end
    return n
end

local function in_task()
    local co = coroutine.running()
    return co ~= nil and co ~= main_co
end

local function wait_impl(t)
    t = tonumber(t) or 0.03
    if t < 0.03 then t = 0.03 end
    if in_task() then
        coroutine.yield(t)
    else
        now = now + t
        run_due()
    end
    return t, now
end

------------------------------------------------------------------------
-- Yieldable pcall/xpcall (Luau allows yielding inside pcall; Lua 5.1 does not)
------------------------------------------------------------------------
local PCALL_LOG = {}      -- message -> count, so repeated failures are summarised

local function note_pcall_error(err, co)
    local msg = tostring(err)
    local c = PCALL_LOG[msg]
    PCALL_LOG[msg] = (c or 0) + 1
    if not c then
        log("PCALL ERR", msg)
        local tb = debug.traceback(co)
        -- keep only lines that point into the script
        for line in tb:gmatch("[^\n]+") do
            if line:find("SCR:%d+:") then real_print("      " .. line) end
        end
    end
end

local function ypcall(f, ...)
    if type(f) ~= "function" and not (type(f) == "table" and getmetatable(f) and getmetatable(f).__call) then
        return false, "attempt to call a " .. type(f) .. " value"
    end
    local co = coroutine.create(function(...) return f(...) end)
    install_budget(co, "pcall")
    local res = { coroutine.resume(co, ...) }
    while true do
        if not res[1] then
            note_pcall_error(res[2], co)
            return false, res[2]
        end
        if coroutine.status(co) == "dead" then
            return true, unpack(res, 2, table.maxn(res))
        end
        -- inner code yielded (a wait): forward it
        if in_task() then
            res = { coroutine.resume(co, coroutine.yield(unpack(res, 2, table.maxn(res)))) }
        else
            now = now + (tonumber(res[2]) or 0.03)
            run_due()
            res = { coroutine.resume(co) }
        end
    end
end

local function yxpcall(f, handler, ...)
    local r = { ypcall(f, ...) }
    if r[1] then return unpack(r, 1, table.maxn(r)) end
    if type(handler) == "function" then return false, handler(r[2]) end
    return false, r[2]
end

------------------------------------------------------------------------
-- Universal proxy: stands in for Instances, services, datatypes, Enums
------------------------------------------------------------------------
local PX = setmetatable({}, { __mode = "k" })   -- proxy -> { name, short, key, parent, store, kids, typ }
local proxy_mt = {}
local MACLIB_PATH = os.getenv("MACLIB_PATH")
local STATS = { FireServer = 0, InvokeServer = 0, HttpGet = {}, Kick = 0 }

local function mkproxy(name, key, parent, typ)
    local p = setmetatable({}, proxy_mt)
    PX[p] = { name = name, short = tostring(key or name), key = key, parent = parent,
              store = {}, kids = {}, typ = typ }
    return p
end
local function is_proxy(v) return PX[v] ~= nil end

local NUM_PROPS = {
    Health = 100, MaxHealth = 100, WalkSpeed = 16, JumpPower = 50, JumpHeight = 7.2, HipHeight = 2,
    Magnitude = 0, X = 0, Y = 0, Z = 0, Scale = 1, Offset = 0, Width = 0, Height = 0,
    R = 0, G = 0, B = 0, Transparency = 0, TimePosition = 0, Length = 1, FieldOfView = 70,
    PlaceId = tonumber(os.getenv("PLACE_ID")) or 2809202155, GameId = 1, UserId = 1, CreatorId = 0,
    AccountAge = 100, ClockTime = 12, Brightness = 1, ZIndex = 1, LayoutOrder = 0, Rotation = 0,
    MaxActivationDistance = 10, HoldDuration = 0, PlaceVersion = 1, MaxPlayers = 20,
    DistributedGameTime = 0, Value = 0, Speed = 1, WeightCurrent = 1,
}
local STR_PROPS = { ClassName = "Part", Text = "", JobId = "mock-job", DisplayName = "Player",
                    Message = "", ContentText = "", Image = "", AnimationId = "", SoundId = "" }
local TRUE_PROPS = { Visible = true, Enabled = true, Anchored = false, CanCollide = true, Archivable = true,
                     Active = true, Playing = false, IsPlaying = false }

local SIGNALS = {}
for nm in ("Heartbeat RenderStepped Stepped PreSimulation PostSimulation PreRender Changed Touched TouchEnded "
        .. "ChildAdded ChildRemoved DescendantAdded DescendantRemoving CharacterAdded CharacterRemoving "
        .. "MessageOut PlayerAdded PlayerRemoving Died HealthChanged InputBegan InputEnded InputChanged Idled "
        .. "MouseButton1Click MouseButton1Down Activated Triggered AncestryChanged FocusLost"):gmatch("%S+") do
    SIGNALS[nm] = true
end
local SIGNAL_MEMBERS = { Connect = true, Once = true, Wait = true, ConnectParallel = true }

proxy_mt.__index = function(p, k)
    local d = PX[p]
    local v = d.store[k]
    if v ~= nil then return v end
    if SIGNALS[d.short] and d.key == d.short and not SIGNAL_MEMBERS[k] then
        error(tostring(k) .. " is not a valid member of RBXScriptSignal", 2)
    end
    if NUM_PROPS[k] then return NUM_PROPS[k] end
    if k == "Name" then return d.short end
    if STR_PROPS[k] then return STR_PROPS[k] end
    if TRUE_PROPS[k] ~= nil then return TRUE_PROPS[k] end
    local kid = d.kids[k]
    if not kid then
        kid = mkproxy(d.name .. "." .. tostring(k), k, p)
        d.kids[k] = kid
    end
    return kid
end
proxy_mt.__newindex = function(p, k, v) PX[p].store[k] = v end
proxy_mt.__tostring = function(p) return PX[p].short end
proxy_mt.__concat = function(a, b) return tostring(a) .. tostring(b) end
local function arith() return mkproxy("arith", "arith", nil, "Vector3") end
proxy_mt.__add, proxy_mt.__sub, proxy_mt.__mul, proxy_mt.__div = arith, arith, arith, arith
proxy_mt.__mod, proxy_mt.__pow, proxy_mt.__unm = arith, arith, arith
proxy_mt.__lt = function() return false end
proxy_mt.__le = function() return false end

local function mock_typeof_early(v)
    local d = PX[v]
    if d and d.typ then return d.typ end
    return "Instance"
end

local function set(list) local t = {} for _, k in ipairs(list) do t[k] = true end return t end
local CHILD_METHODS = set{ "FindFirstChild", "WaitForChild", "FindFirstChildOfClass", "FindFirstChildWhichIsA",
    "FindFirstAncestor", "FindFirstAncestorOfClass", "FindFirstAncestorWhichIsA", "FindFirstDescendant" }
local LIST_METHODS = set{ "GetChildren", "GetDescendants", "GetPlayers", "GetTagged", "GetTags", "GetPartsInPart",
    "GetPartBoundsInRadius", "GetPartBoundsInBox", "GetTouchingParts", "GetPlayingAnimationTracks",
    "GetConnectedParts", "GetJoints", "GetKeysPressed", "GetMouseButtonsPressed", "GetAttributes",
    "GetFriendsOnline", "GetGuiObjectsAtPosition", "GetEnumItems", "getconnections" }
local TRUE_METHODS = set{ "IsA", "IsDescendantOf", "IsAncestorOf", "IsLoaded", "IsClient" }
local FALSE_METHODS = set{ "IsKeyDown", "IsMouseButtonPressed", "IsStudio", "IsServer" }
local CONNECT_METHODS = set{ "Connect", "Once", "ConnectParallel" }

local FRAME_SIGNALS = { Heartbeat = true, RenderStepped = true, Stepped = true,
                  PreSimulation = true, PostSimulation = true, PreRender = true }
local FRAME_CONNS = {}
local ABSENT = {}
for nm in (os.getenv("ABSENT") or ""):gmatch("[^,]+") do ABSENT[nm] = true end

-- Only members that look like methods/constructors are callable; calling an Instance,
-- property or child errors as in Roblox.
local METHOD_PREFIXES = { "Get", "Set", "Find", "Is", "Wait", "Connect", "Once", "Fire", "Invoke",
    "Destroy", "Clone", "Play", "Stop", "Pause", "Resume", "Cancel", "Create", "new", "from", "Load",
    "Remove", "Clear", "Move", "Pivot", "Apply", "Add", "Raycast", "To", "Lerp", "Kick", "Teleport",
    "Capture", "Disconnect", "Http", "JSON", "Generate", "Url", "Request", "Change", "Equip", "Unequip",
    "Take", "Activate", "Click", "Press", "Send", "Release", "Query", "Inverse", "Dot", "Cross", "Bind",
    "Unbind", "Preload", "Compute", "Navigate", "Break", "Make", "Update", "Refresh", "Notify",
    "Dialog", "Unload", "Show", "Hide", "Select", "Tween", "Emit", "Angles", "lookAt", "Fuzzy",
    "Point", "Vector", "Components", "Abs", "Max", "Min", "Floor", "Ceil", "Sign", "Insert", "Has",
    "Can", "Check", "Track", "Adjust", "Render", "Simulate", "Spawn", "Interrupt", "Delete", "Write",
    "Read", "Open", "Close", "Toggle", "Lock", "Unlock", "Reset", "Start", "End", "Begin", "Next" }
local function is_callable(d)
    if d.name:sub(1, 6) == "MacLib" then return true end
    local key = tostring(d.key)
    for _, pre in ipairs(METHOD_PREFIXES) do
        if key:sub(1, #pre) == pre then return true end
    end
    return false
end

proxy_mt.__call = function(p, ...)
    local d = PX[p]
    local key = tostring(d.key)
    local self = (...)
    if not is_callable(d) then
        local at, lvl = "", 2
        while true do
            local info = debug.getinfo(lvl, "Sl")
            if not info then break end
            if info.short_src == "SCR" then at = "SCR:" .. info.currentline .. ": " break end
            lvl = lvl + 1
        end
        error(at .. "attempt to call a " .. mock_typeof_early(p) .. " value", 0)
    end
    if CHILD_METHODS[key] then
        local name = select(2, ...)
        if ABSENT[tostring(name)] then return nil end
        if is_proxy(self) and name ~= nil then return self[tostring(name)] end
        return nil
    elseif key == "GetService" then
        return is_proxy(self) and self[tostring(select(2, ...))] or nil
    elseif LIST_METHODS[key] then
        return {}
    elseif TRUE_METHODS[key] then
        return true
    elseif FALSE_METHODS[key] then
        return false
    elseif CONNECT_METHODS[key] then
        local conn = mkproxy("Connection", "Connection", nil, "RBXScriptConnection")
        local sig = d.parent and PX[d.parent]
        local fn = select(2, ...)
        if sig and FRAME_SIGNALS[sig.short] and type(fn) == "function" then
            FRAME_CONNS[conn] = fn
        end
        return conn
    elseif key == "Disconnect" then
        if self ~= nil then FRAME_CONNS[self] = nil end
        return nil
    elseif key == "Wait" then
        wait_impl(0.03)
        return mkproxy("waited", "waited")
    elseif key == "HttpGet" or key == "HttpGetAsync" then
        local url = tostring(select(2, ...))
        STATS.HttpGet[#STATS.HttpGet + 1] = url
        if url:lower():find("maclib", 1, true) and MACLIB_PATH then
            local f = assert(io.open(MACLIB_PATH, "rb"))
            local s = f:read("*a")
            f:close()
            return s
        end
        error("HttpGet mock: no content for " .. url, 2)
    elseif key == "JSONEncode" then
        return "{}"
    elseif key == "JSONDecode" then
        return {}
    elseif key == "GenerateGUID" then
        return "00000000-0000-0000-0000-000000000000"
    elseif key == "FireServer" or key == "InvokeServer" or key == "Kick" then
        STATS[key] = STATS[key] + 1
        return nil
    elseif key == "Teleport" or key == "TeleportToPlaceInstance" or key == "TeleportAsync"
            or key == "TeleportToSpawnByName" then
        STATS.Teleport = (STATS.Teleport or 0) + 1
        local tb = {}
        for line in debug.traceback("", 2):gmatch("[^\n]+") do
            if line:find("SCR:%d+:") then tb[#tb + 1] = line:match("SCR:%d+") end
        end
        log("TELEPORT", key .. " called from " .. table.concat(tb, " < "))
        return nil
    elseif key == "GetAttribute" then
        return nil
    end
    local pd = d.parent and PX[d.parent]
    local typ = nil
    if pd and (key == "new" or key:match("^from") or key == "lookAt" or key == "Angles") then
        typ = (pd.short == "Instance") and "Instance" or pd.short
    end
    return mkproxy(d.name .. "()", key .. "()", nil, typ)
end

local function mock_typeof(v)
    local d = PX[v]
    if not d then return type(v) end
    if d.typ then return d.typ end
    if d.name:match("^Enum%.[^.]+%.[^.]+$") then return "EnumItem" end
    if d.name:match("^Enum%.[^.]+$") then return "Enum" end
    return "Instance"
end

------------------------------------------------------------------------
-- Fake MacLib: every call is a proxy; Window creation is logged
------------------------------------------------------------------------
local UI_CALLS = {}
local UI_ELEMS = {}   -- { kind, name, cb } for every MacLib element with a Callback
local real_call = proxy_mt.__call
proxy_mt.__call = function(p, ...)
    local d = PX[p]
    if d.name:sub(1, 6) == "MacLib" then
        local key = tostring(d.key)
        UI_CALLS[key] = (UI_CALLS[key] or 0) + 1
        if key == "Window" then log("UI", "MacLib Window() called") end
        for i = 1, select("#", ...) do
            local a = select(i, ...)
            if type(a) == "table" and not PX[a] and type(rawget(a, "Callback")) == "function" then
                UI_ELEMS[#UI_ELEMS + 1] = { kind = key, cb = rawget(a, "Callback"),
                    name = tostring(rawget(a, "Name") or rawget(a, "Title") or "?") }
            end
        end
    end
    return real_call(p, ...)
end

------------------------------------------------------------------------
-- In-memory filesystem; the boot log is echoed to stdout
------------------------------------------------------------------------
local FS, DIRS = {}, {}
local function fs_write(path, s)
    FS[path] = s
end
local function fs_append(path, s)
    FS[path] = (FS[path] or "") .. s
    if path:find("boot_log", 1, true) then log("BOOT", (s:gsub("\n$", ""))) end
end

------------------------------------------------------------------------
-- Globals
------------------------------------------------------------------------
local G = _G
local game = mkproxy("game", "game")
G.game, G.Game = game, game
G.workspace, G.Workspace = game.Workspace, game.Workspace
for _, nm in ipairs({ "Enum", "Instance", "Vector3", "Vector2", "CFrame", "UDim2", "UDim", "Color3",
    "ColorSequence", "ColorSequenceKeypoint", "NumberSequence", "NumberSequenceKeypoint", "NumberRange",
    "TweenInfo", "Ray", "RaycastParams", "OverlapParams", "BrickColor", "Rect", "PhysicalProperties",
    "Region3", "Font", "DateTime", "Random", "Axes", "Faces", "script", "Drawing" }) do
    G[nm] = mkproxy(nm, nm)
end
G.shared = {}
G.typeof = mock_typeof
G.tick, G.time, G.elapsedTime = clock, clock, clock
os.clock = clock
G.wait = function(t) return wait_impl(t) end
G.spawn = function(f) return schedule(0, f) end
G.delay = function(t, f) return schedule(t, f) end
G.task = {
    wait = wait_impl,
    spawn = function(f, ...) if type(f) == "thread" then return f end return spawn_now(f, ...) end,
    defer = function(f, ...) return schedule(0, f, ...) end,
    delay = function(t, f, ...) return schedule(t, f, ...) end,
    cancel = function() end,
    synchronize = function() end,
    desynchronize = function() end,
}
G.pcall, G.xpcall = ypcall, yxpcall
local real_wrap = coroutine.wrap
coroutine.wrap = function(f) return function(...) return spawn_now(f, ...) end end
G.print = function(...) local t = {} for i = 1, select("#", ...) do t[i] = tostring((select(i, ...))) end log("PRINT", table.concat(t, " ")) end
G.warn = function(...) local t = {} for i = 1, select("#", ...) do t[i] = tostring((select(i, ...))) end log("WARN", table.concat(t, " ")) end

table.find = function(t, v, init) for i = init or 1, #t do if t[i] == v then return i end end return nil end
table.clear = function(t) for k in pairs(t) do t[k] = nil end end
table.create = function(n, v) local t = {} for i = 1, n do t[i] = v end return t end
table.freeze = function(t) return t end
table.isfrozen = function() return false end
table.unpack = unpack
table.pack = function(...) return { n = select("#", ...), ... } end
string.split = function(s, sep)
    sep = sep or ","
    local out, start = {}, 1
    while true do
        local a, b = s:find(sep, start, true)
        if not a then out[#out + 1] = s:sub(start) break end
        out[#out + 1] = s:sub(start, a - 1)
        start = b + 1
    end
    return out
end
math.clamp = function(x, lo, hi) if x < lo then return lo elseif x > hi then return hi end return x end
math.round = function(x) return math.floor(x + 0.5) end
math.sign = function(x) return x > 0 and 1 or (x < 0 and -1 or 0) end
math.noise = function() return 0 end

-- executor API
local noop = function() end
G.getgenv = function() return G end
G.getrenv = function() return {} end
G.getsenv = function() return {} end
G.getreg = function() return {} end
G.getgc = function() return {} end
G.getinstances = function() return {} end
G.getconnections = function() return {} end
G.get_signal_cons = G.getconnections
G.hookmetamethod = function() return function() return nil end end
G.hookfunction = function(f) return f end
G.newcclosure = function(f) return f end
G.clonefunction = function(f) return f end
G.checkcaller = function() return true end
G.getcallingscript = function() return nil end
G.getnamecallmethod = function() return "" end
G.getrawmetatable = function() return { __namecall = noop, __index = noop, __newindex = noop } end
G.setreadonly, G.make_writeable = noop, noop
G.fireproximityprompt, G.firesignal, G.fireclickdetector, G.firetouchinterest = noop, noop, noop, noop
G.setclipboard, G.toclipboard, G.queue_on_teleport, G.openbrowser = noop, noop, noop, noop
G.setfpscap = noop
G.identifyexecutor = function() return "MockExec", "1.0" end
G.gethui = function() return game.CoreGui end
G.get_hidden_gui = G.gethui
G.cloneref = function(x) return x end
G.getcustomasset = function(p) return "rbxasset://" .. tostring(p) end
G.getsynasset = G.getcustomasset
G.request = function() return { StatusCode = 200, Success = true, Body = "{}", Headers = {} } end
G.http_request = G.request
G.readfile = function(p) local s = FS[p] if s == nil then error("file not found: " .. tostring(p), 2) end return s end
G.writefile = fs_write
G.appendfile = fs_append
G.isfile = function(p) return FS[p] ~= nil end
G.isfolder = function(p) return DIRS[p] == true end
G.makefolder = function(p) DIRS[p] = true end
G.delfile = function(p) FS[p] = nil end
G.listfiles = function() return {} end

-- report reads of undefined globals once each
local MISSING = {}
setmetatable(G, { __index = function(_, k)
    if type(k) == "string" and not MISSING[k] and not k:match("^_[rp]%d+$") then
        MISSING[k] = true
        log("MISSING GLOBAL", k)
    end
    return nil
end })

------------------------------------------------------------------------
-- loadstring: MacLib source (Luau syntax) is replaced by the fake
------------------------------------------------------------------------
local real_loadstring = loadstring
rawset(G, "loadstring", function(src, name)
    if type(src) == "string" and #src > 100000 and src:find("MacLib", 1, true) then
        log("UI", "loadstring(MacLib, " .. #src .. " bytes) -> fake MacLib")
        return function() return mkproxy("MacLib", "MacLib") end
    end
    local f, err = real_loadstring(src, name)
    if not f then log("LOADSTRING FAIL", err) end
    return f, err
end)

------------------------------------------------------------------------
-- Run
------------------------------------------------------------------------
local fh = assert(io.open(SCRIPT, "rb"))
local src = fh:read("*a")
fh:close()
local chunk, cerr = real_loadstring(src, "=SCR")
if not chunk then log("FATAL", cerr) os.exit(1) end
install_budget(nil, "main")
local ok, e = xpcall(chunk, debug.traceback)
log("MAIN", ok and "main chunk returned normally" or ("main chunk died: " .. tostring(e)))
for _ = 1, tonumber(os.getenv("DRAIN_STEPS")) or 100 do
    now = now + 0.5
    for _, fn in pairs(FRAME_CONNS) do spawn_now(fn, 0.016) end
    run_due()
end
-- EVAL=<lua>: run code in the script's global environment after boot (results printed by the code)
local EVAL = os.getenv("EVAL")
if EVAL then
    local f, err = real_loadstring(EVAL, "=EVAL")
    if not f then log("EVAL", "compile error: " .. tostring(err)) else
        local ok2, e2 = pcall(f)
        if not ok2 then log("EVAL", "error: " .. tostring(e2)) end
    end
end

-- FIRE=<pattern>: switch on matching toggles / press matching buttons (case-insensitive plain
-- match on the element name, "*" = all), then run FIRE_STEPS half-second steps.
local FIRE = os.getenv("FIRE")
if FIRE then
    LOG_AGG = true
    local pat = FIRE:lower()
    for _, el in ipairs(UI_ELEMS) do
        if (pat == "*" or el.name:lower():find(pat, 1, true)) and (el.kind == "Toggle" or el.kind == "Button") then
            log("FIRE", el.kind .. " '" .. el.name .. "'")
            if el.kind == "Toggle" then spawn_now(el.cb, true) else spawn_now(el.cb) end
        end
    end
    for _ = 1, tonumber(os.getenv("FIRE_STEPS")) or 120 do
        now = now + 0.5
        for _, fn in pairs(FRAME_CONNS) do spawn_now(fn, 0.016) end
        run_due()
    end
    local rep = {}
    for s, c in pairs(LOG_COUNTS) do if c > 1 then rep[#rep + 1] = string.format("%6d x %s", c, s) end end
    table.sort(rep, function(a, b) return tonumber(a:match("%d+")) > tonumber(b:match("%d+")) end)
    log("SUMMARY", #rep .. " repeated output lines after FIRE (count x line):")
    for i = 1, math.min(#rep, 40) do real_print("   " .. rep[i]) end
end
local uc = {}
for k, v in pairs(UI_CALLS) do uc[#uc + 1] = k .. "=" .. v end
table.sort(uc)
log("SUMMARY", "MacLib calls: " .. table.concat(uc, " "))
log("SUMMARY", string.format("FireServer=%d InvokeServer=%d Kick=%d Teleport=%d HttpGet=%d live tasks=%d",
    STATS.FireServer, STATS.InvokeServer, STATS.Kick, STATS.Teleport or 0, #STATS.HttpGet, #tasks))
local pe = {}
for msg, c in pairs(PCALL_LOG) do pe[#pe + 1] = string.format("%5d x %s", c, msg) end
table.sort(pe)
log("SUMMARY", #pe .. " distinct pcall errors")
local te = {}
for msg, c in pairs(TASK_ERRS) do te[#te + 1] = string.format("%5d x %s", c, msg) end
table.sort(te)
log("SUMMARY", #te .. " distinct task errors")
for _, l in ipairs(te) do real_print("   " .. l) end
local nf = 0
for _ in pairs(FRAME_CONNS) do nf = nf + 1 end
log("SUMMARY", nf .. " frame-signal connections active")
for _, l in ipairs(pe) do real_print("   " .. l) end
