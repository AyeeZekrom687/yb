-- Console log: mirrors the client console (prints, warnings, errors) to YBA/console_log_<UserId>.txt.
-- Inserted at the top of scr by the build step. Writes are buffered and flushed once per second;
-- each distinct message is written at most REPEAT_LIMIT times, later copies are counted and
-- summarised every SUMMARY_INTERVAL seconds.
do
    local FOLDER = "YBA"
    -- one log per account (several accounts on one PC overwrote each other's log)
    local PATH = FOLDER .. "/console_log.txt"
    pcall(function()
        local lp = game:GetService("Players").LocalPlayer
        if lp and lp.UserId ~= 0 then
            PATH = FOLDER .. "/console_log_" .. lp.UserId .. ".txt"
        end
    end)
    local REPEAT_LIMIT = 3
    local FLUSH_INTERVAL = 1
    local SUMMARY_INTERVAL = 30
    local MAX_KEYS = 2000

    local env = (type(getgenv) == "function" and getgenv()) or _G
    local append = appendfile
    if type(append) ~= "function" and type(readfile) == "function" and type(isfile) == "function" then
        append = function(p, s)
            writefile(p, (isfile(p) and readfile(p) or "") .. s)
        end
    end

    if type(writefile) == "function" and type(append) == "function" then
        if type(env._YBA_ConsoleLogStop) == "function" then
            pcall(env._YBA_ConsoleLogStop)
        end
        local gen = {}
        env._YBA_ConsoleLogGen = gen

        local typeNames = {
            [Enum.MessageType.MessageOutput] = "OUT",
            [Enum.MessageType.MessageInfo] = "INFO",
            [Enum.MessageType.MessageWarning] = "WARN",
            [Enum.MessageType.MessageError] = "ERROR",
        }
        local buffer, counts, suppressed, nkeys = {}, {}, {}, 0

        local function record(msg, mtype, time)
            local key = "[" .. (typeNames[mtype] or "?") .. "] " .. tostring(msg)
            local c = counts[key]
            if not c then
                if nkeys >= MAX_KEYS then
                    counts, nkeys = {}, 0
                end
                nkeys = nkeys + 1
                c = 0
            end
            c = c + 1
            counts[key] = c
            if c <= REPEAT_LIMIT then
                buffer[#buffer + 1] = "[" .. os.date("%H:%M:%S", time) .. "] " .. key .. "\n"
            else
                suppressed[key] = (suppressed[key] or 0) + 1
            end
        end

        local function flush()
            if #buffer > 0 then
                local data = table.concat(buffer)
                buffer = {}
                pcall(append, PATH, data)
            end
        end

        local function summarise()
            local stamp = os.date("%H:%M:%S")
            for key, n in pairs(suppressed) do
                buffer[#buffer + 1] = "[" .. stamp .. "] " .. key .. "  (repeated " .. n .. " more times)\n"
            end
            suppressed = {}
        end

        local ok = pcall(function()
            if type(isfolder) == "function" and type(makefolder) == "function" and not isfolder(FOLDER) then
                makefolder(FOLDER)
            end
            writefile(PATH, "=== YBA console log, started " .. os.date("%Y-%m-%d %H:%M:%S") .. " ===\n")
            local LogService = game:GetService("LogService")
            local hok, history = pcall(function() return LogService:GetLogHistory() end)
            if hok and type(history) == "table" and #history > 0 then
                buffer[#buffer + 1] = "--- console history before this run ---\n"
                for _, entry in ipairs(history) do
                    record(entry.message, entry.messageType, tonumber(entry.timestamp))
                end
                buffer[#buffer + 1] = "--- live output ---\n"
            end
            flush()
            local conn = LogService.MessageOut:Connect(function(msg, mtype)
                record(msg, mtype)
            end)
            env._YBA_ConsoleLogStop = function()
                env._YBA_ConsoleLogGen = nil
                conn:Disconnect()
                summarise()
                flush()
            end
        end)

        if ok then
            task.spawn(function()
                local lastSummary = os.clock()
                while env._YBA_ConsoleLogGen == gen do
                    task.wait(FLUSH_INTERVAL)
                    if env._YBA_ConsoleLogGen ~= gen then
                        break
                    end
                    if next(suppressed) ~= nil and os.clock() - lastSummary >= SUMMARY_INTERVAL then
                        lastSummary = os.clock()
                        summarise()
                    end
                    flush()
                end
            end)
        end
    end
end
