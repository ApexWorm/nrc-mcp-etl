-- Read-only ET: Legacy bot observer. Prints every bot client's position, team and stance at a
-- fixed interval so the Python driver can tell whether bots move along their waypoints.
-- No gentity_set, console commands or damage happen here.
local token = "__NRC_TOKEN__"
local interval = __NRC_INTERVAL__
local nextSample = 0
local SVF_BOT = 8          -- game/bg_public.h:157
local EF_DEAD = 1          -- game/bg_public.h:725
local EF_CROUCHING = 16    -- game/bg_public.h:730
local EF_PRONE = 524288    -- game/bg_public.h:752
local teams = {[1] = "red", [2] = "blue", [3] = "spectator"}
local function read(n, key)
    local ok, value = pcall(et.gentity_get, n, key)
    if ok then return value end
    return nil
end
local function num(v)
    return string.format("%.1f", v or 0)
end
function et_RunFrame(time)
    if time < nextSample then return end
    nextSample = time + interval
    local rows = {}
    local maxclients = tonumber(et.trap_Cvar_Get("sv_maxclients")) or 64
    for n = 0, maxclients - 1 do
        if read(n, "inuse") == 1 and (read(n, "r.svFlags") or 0) & SVF_BOT ~= 0 then
            local o = read(n, "ps.origin") or {}
            local flags = read(n, "ps.eFlags") or 0
            local stance = "stand"
            if flags & EF_PRONE ~= 0 then stance = "prone" elseif flags & EF_CROUCHING ~= 0 then stance = "crouch" end
            rows[#rows + 1] = "{\"client\":" .. n .. ",\"team\":\"" .. (teams[read(n, "sess.sessionTeam")] or "none")
                .. "\",\"health\":" .. (read(n, "health") or 0)
                .. ",\"dead\":" .. tostring(flags & EF_DEAD ~= 0)
                .. ",\"stance\":\"" .. stance .. "\",\"position\":[" .. num(o[1]) .. "," .. num(o[2]) .. "," .. num(o[3]) .. "]}"
        end
    end
    -- et.G_Print keeps 1023 bytes (src/game/g_lua.c:193): print in pieces, end the line once.
    local line = "NRC_BOTS " .. token .. " {\"time\":" .. time .. ",\"bots\":[" .. table.concat(rows, ",") .. "]}"
    for i = 1, #line, 1000 do
        et.G_Print(string.sub(line, i, i + 999))
    end
    et.G_Print("\n")
end
