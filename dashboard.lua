-- [port] Low Render Mode (user request): 3D rendering off (RunService:Set3dRenderingEnabled) and a
-- dashboard instead, updated once a second: what is running, stand, items, money, recent
-- messages. It only reads the local player's data (no remote calls). The dashboard sits above
-- the game's HUD and below the hub (toggle button 50, MacLib window above), and takes no input,
-- so clicks the farms send to dialogue buttons still reach them.
do
  local D = {
    enabled = false,
    gen = 0,              -- update loop generation: one loop per enable
    activity = {},        -- recent notifications, newest last
    MAX_ACTIVITY = 8,
    gui = nil,
    labels = nil,
    startTime = 0,        -- session start (when the dashboard was switched on)
    startMoney = nil
  }
  getgenv()._YBA_Dashboard = D

  -- fed by the hub's notify function: every farm's status messages pass through it
  function D.log(title, text)
    local list = D.activity
    list[#list + 1] = os.date("%H:%M:%S") .. "  " .. tostring(title) .. ": " .. tostring(text)
    if #list > D.MAX_ACTIVITY then
      table.remove(list, 1)
    end
  end

  local function newLabel(parent, order, font, size, color)
    local t = Instance.new("TextLabel")
    t.BackgroundTransparency = 1
    t.Size = UDim2.new(1, 0, 0, 0)
    t.AutomaticSize = Enum.AutomaticSize.Y
    t.TextXAlignment = Enum.TextXAlignment.Left
    t.TextYAlignment = Enum.TextYAlignment.Top
    t.TextWrapped = true
    t.Font = font
    t.TextSize = size
    t.TextColor3 = color or Color3.fromRGB(225, 225, 232)
    t.LayoutOrder = order
    t.Text = ""
    t.Parent = parent
    return t
  end

  -- full-screen background with a centred column of sections; returns the gui and its labels
  local function build()
    local gui = Instance.new("ScreenGui")
    gui.Name = "YBAThingDashboard"
    gui.ResetOnSpawn = false
    gui.IgnoreGuiInset = true
    gui.ZIndexBehavior = Enum.ZIndexBehavior.Sibling
    gui.DisplayOrder = 40
    local bg = Instance.new("Frame")
    bg.Size = UDim2.fromScale(1, 1)
    bg.BackgroundColor3 = Color3.fromRGB(14, 14, 18)
    bg.BorderSizePixel = 0
    bg.Active = false
    bg.Parent = gui
    local panel = Instance.new("Frame")
    panel.AnchorPoint = Vector2.new(0.5, 0.5)
    panel.Position = UDim2.fromScale(0.5, 0.5)
    panel.Size = UDim2.new(0.9, 0, 0, 0)
    panel.AutomaticSize = Enum.AutomaticSize.Y
    panel.BackgroundTransparency = 1
    panel.Parent = bg
    local limit = Instance.new("UISizeConstraint")
    limit.MaxSize = Vector2.new(640, math.huge)
    limit.Parent = panel
    local list = Instance.new("UIListLayout")
    list.Padding = UDim.new(0, 10)
    list.SortOrder = Enum.SortOrder.LayoutOrder
    list.Parent = panel
    local title = newLabel(panel, 1, Enum.Font.GothamBold, 22, Color3.fromRGB(150, 180, 255))
    title.Text = "YBA Thing - Low Render Mode"
    local labels = {
      doing = newLabel(panel, 2, Enum.Font.Gotham, 17),
      player = newLabel(panel, 3, Enum.Font.Gotham, 17),
      stand = newLabel(panel, 4, Enum.Font.Gotham, 17),
      items = newLabel(panel, 5, Enum.Font.RobotoMono, 15),
      activity = newLabel(panel, 6, Enum.Font.RobotoMono, 13, Color3.fromRGB(160, 160, 170))
    }
    local ok = pcall(function()
      gui.Parent = gethui and gethui() or game:GetService("CoreGui")
    end)
    if not ok or not gui.Parent then
      pcall(function()
        gui.Parent = L2_1:FindFirstChild("PlayerGui")
      end)
    end
    local runtime = getgenv().__YBA_RUNTIME
    if type(runtime) == "table" and type(runtime.Guis) == "table" then
      runtime.Guis[#runtime.Guis + 1] = gui
    end
    return gui, labels
  end

  local function stat(ps, name)
    local v = ps and ps:FindFirstChild(name)
    return v and v.Value
  end

  -- 1234567 -> "1,234,567"
  local function money(n)
    local s = tostring(math.floor(math.abs(n)))
    s = s:reverse():gsub("(%d%d%d)", "%1,"):reverse():gsub("^,", "")
    return (n < 0 and "-" or "") .. s
  end

  local function duration(sec)
    sec = math.max(0, math.floor(sec))
    return string.format("%d:%02d:%02d", math.floor(sec / 3600), math.floor(sec / 60) % 60, sec % 60)
  end

  -- features on, from the coordinator (whose turn it is, who waits)
  local function doingText()
    local C = getgenv()._YBA_Coord
    if not C then
      return "Doing: -"
    end
    if not C.enabled then
      return "Doing: (Coordinate Farms is off, so turns are not tracked)"
    end
    local parts = {}
    for name, isOn in pairs(C.active) do
      local ok, on = pcall(isOn)
      if ok and on then
        local state = C.holder == name and "running" or C.waiting[name] and "waiting its turn" or "on"
        parts[#parts + 1] = (C.names[name] or name) .. " (" .. state .. ")"
      end
    end
    table.sort(parts)
    return "Doing: " .. (#parts > 0 and table.concat(parts, ", ") or "no farm running")
  end

  local function playerText(ps)
    local lines = {
      string.format("Prestige %s   Level %s", tostring(stat(ps, "Prestige") or "?"), tostring(stat(ps, "Level") or "?"))
    }
    local m = tonumber(stat(ps, "Money"))
    if m then
      D.startMoney = D.startMoney or m
      local gained = m - D.startMoney
      lines[#lines + 1] = "Money: $" .. money(m) .. "   (" .. (gained < 0 and "" or "+") .. money(gained) .. " this session)"
    end
    lines[#lines + 1] = "Session: " .. duration(tick() - D.startTime)
    return table.concat(lines, "\n")
  end

  -- the skin is the stand farm's latest check (no remote call here)
  local function standText(ps)
    local stand = tostring(stat(ps, "Stand") or "None")
    local s = "Stand: " .. stand
    local skin = getgenv()._YBA_LastSkin
    if skin and skin.stand == stand and skin.skin and skin.skin ~= "None" and skin.skin ~= "" then
      s = s .. "  [" .. tostring(skin.skin) .. "]"
    end
    local spec = stat(ps, "Spec")
    if spec and spec ~= "None" and spec ~= "" then
      s = s .. "\nSpec: " .. tostring(spec)
    end
    local st = getgenv()._YBA_SFStats
    if st and (st.standsGot or 0) > 0 then
      s = s .. string.format("\nStand farm (this server): %d stands, %d arrows, %d lucky arrows, %d Rokakaka",
        st.standsGot, st.arrowsUsed or 0, st.luckyArrowsUsed or 0, st.rokasUsed or 0)
    end
    return s
  end

  -- every Tool in the backpack and the character, counted by name, in two columns
  local function itemsText()
    local counts, names = {}, {}
    local boxes = {L2_1:FindFirstChild("Backpack"), L2_1.Character}
    for i = 1, 2 do
      local box = boxes[i]
      for _, tool in ipairs(box and box:GetChildren() or {}) do
        if tool:IsA("Tool") then
          local n = tool.Name
          if not counts[n] then
            names[#names + 1] = n
          end
          counts[n] = (counts[n] or 0) + 1
        end
      end
    end
    if #names == 0 then
      return "Items: none"
    end
    table.sort(names)
    local half = math.ceil(#names / 2)
    local lines = {"Items:"}
    for i = 1, half do
      local left = string.format("%3d %-31s", counts[names[i]], names[i])
      local right = names[i + half] and string.format("%3d %s", counts[names[i + half]], names[i + half]) or ""
      lines[#lines + 1] = left .. right
    end
    return table.concat(lines, "\n")
  end

  local function activityText()
    if #D.activity == 0 then
      return "Recent: (nothing yet)"
    end
    return "Recent:\n" .. table.concat(D.activity, "\n")
  end

  local function setText(label, text)
    if label.Text ~= text then
      label.Text = text
    end
  end

  local function update()
    local L = D.labels
    local ps = L2_1:FindFirstChild("PlayerStats")
    setText(L.doing, doingText())
    setText(L.player, playerText(ps))
    setText(L.stand, standText(ps))
    setText(L.items, itemsText())
    setText(L.activity, activityText())
  end

  -- on: 3D rendering off, dashboard shown and updated every second; off: both restored.
  -- getgenv()._YBA_LowRender pauses the visual-only loops the dashboard covers (HUD health text,
  -- watermark, ESP refresh, weather); the hub's acrylic blur (per-frame work while the window is
  -- open) is off meanwhile and restored after.
  function D.setEnabled(on)
    on = on and true or false
    if on == D.enabled then
      return
    end
    D.enabled = on
    D.gen = D.gen + 1
    getgenv()._YBA_LowRender = on
    pcall(function()
      L6_1:Set3dRenderingEnabled(not on)
    end)
    pcall(function()
      local win = getgenv().RAILhubWindow
      if not win then
        return
      end
      if on then
        D.blurBefore = win:GetAcrylicBlurState()
        win:SetAcrylicBlurState(false)
      elseif D.blurBefore ~= nil then
        win:SetAcrylicBlurState(D.blurBefore)
        D.blurBefore = nil
      end
    end)
    if not on then
      if D.gui then
        D.gui.Enabled = false
      end
      return
    end
    if not (D.gui and D.gui.Parent) then
      D.gui, D.labels = build()
    end
    D.gui.Enabled = true
    D.startTime = tick()
    D.startMoney = nil
    local gen = D.gen
    task.spawn(function()
      while D.enabled and D.gen == gen do
        pcall(update)
        task.wait(1)
      end
    end)
  end
end
