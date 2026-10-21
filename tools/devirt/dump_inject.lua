-- Injected in place of the VM entry call: walks the proto tree with the VM's own decoders and
-- writes one JSON object per function (no hub code runs).
local OUT = assert(io.open(DUMP_PATH, "w"))
local function jstr(s)
    local t = {}
    for i = 1, #s do t[i] = string.byte(s, i) end
    return "[" .. table.concat(t, ",") .. "]"
end
local function jconst(v)
    local t = type(v)
    if t == "nil" then return '{"t":"nil"}' end
    if t == "boolean" then return '{"t":"bool","v":' .. tostring(v) .. '}' end
    if t == "number" then return '{"t":"num","v":' .. string.format("%.17g", v) .. '}' end
    if t == "string" then return '{"t":"str","b":' .. jstr(v) .. '}' end
    return '{"t":"' .. t .. '"}'
end
local NEXT_ID = 0
local function dump(P, parent, index)
    NEXT_ID = NEXT_ID + 1
    local id = NEXT_ID
    local src_k, num_k = P[21], P[27]
    local key1 = rEwLIrRX(P[29], rEwLIrRX(P[33], P[14]))
    local step = rEwLIrRX(P[24], P[30])
    local base = P[17]
    local kop = rEwLIrRX(P[2], rEwLIrRX(P[33], 0xA5))
    local ka = rEwLIrRX(P[3], rEwLIrRX(P[30], 0x5A))
    key1 = (key1 + MRLtTPsN) % 256
    local consts = {}
    for i = 1, num_k do
        local v = src_k[i]
        if type(v) == "string" then v = Av5acpsWk(v) end
        consts[i] = jconst(v)
    end
    local code = P[23]
    local n = #code / 9
    local ins = {}
    for i = 1, n do
        local pos = (i - 1) * 9 + 1
        local b1, b2, b3, b4, b5, b6, b7, b8, b9 = XdbzhdpC(code, pos, pos + 8)
        local k = (key1 + i * step) % 256
        local op = (b3 - k) % 256
        local a = (b5 - k) % 256
        local b = rEwLIrRX(b9, k) * 256 + rEwLIrRX(b2, k)
        local c = rEwLIrRX(b1, k) * 256 + rEwLIrRX(b7, k)
        local bx = rEwLIrRX(b4, k) * 65536 + rEwLIrRX(b6, k) * 256 + rEwLIrRX(b8, k)
        ins[i] = "[" .. op .. "," .. a .. "," .. b .. "," .. c .. "," .. bx .. "]"
    end
    local subs = P[16] or {}
    OUT:write('{"id":' .. id .. ',"parent":' .. (parent or 0) .. ',"index":' .. (index or 0)
        .. ',"numparams":' .. tostring(P[33]) .. ',"vararg":' .. tostring(P[8])
        .. ',"nups":' .. tostring(P[14]) .. ',"base":' .. tostring(base)
        .. ',"nsubs":' .. #subs .. ',"consts":[' .. table.concat(consts, ",")
        .. '],"code":[' .. table.concat(ins, ",") .. ']}\n')
    for i = 1, #subs do dump(subs[i], id, i) end
end
dump(MFHaBhX24T, nil, nil)
OUT:close()
return
