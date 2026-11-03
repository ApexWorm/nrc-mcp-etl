-- Read-only ET: Legacy observer. The Python driver sends ordinary client commands.
-- No gentity_set, console commands, damage, script events or weapon grants occur here.
local token = "__NRC_TOKEN__"
local watched = __NRC_WATCHED__
local seen = {}
local nextSample = 0
local ack = 0
local teams = {[1] = "red", [2] = "blue", [3] = "spectator"}
local roles = {[0] = "soldier", [1] = "medic", [2] = "engineer", [3] = "fieldops", [4] = "covertops"}
local weapons = {[21] = "pliers", [15] = "dynamite", [1] = "knife"}
local phases = {[0] = "playing", [1] = "warmup", [2] = "waiting", [3] = "intermission"}
local function read(n, key, index)
    local ok, value = pcall(et.gentity_get, n, key, index)
    if ok then return value end
    return nil
end
local function vector(value)
    if type(value) ~= "table" then return nil end
    return {value[1], value[2], value[3]}
end
local function quote(s)
    return '"' .. tostring(s):gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t') .. '"'
end
local function json(value)
    if value == nil then return "null" end
    if type(value) == "boolean" or type(value) == "number" then return tostring(value) end
    if type(value) == "string" then return quote(value) end
    if type(value) ~= "table" then return "null" end
    local rows = {}
    if value[1] ~= nil then
        for _, v in ipairs(value) do rows[#rows + 1] = json(v) end
        return "[" .. table.concat(rows, ",") .. "]"
    end
    for k, v in pairs(value) do rows[#rows + 1] = quote(k) .. ":" .. json(v) end
    return "{" .. table.concat(rows, ",") .. "}"
end
local function sample(time)
    local entities = {}
    for name in pairs(watched) do entities[name] = {present = false, seen = seen[name] == true, count = 0} end
    local charges, armed, chargeAt = 0, 0, nil
    for n = 64, 1021 do
        if read(n, "inuse") == 1 then
            if read(n, "s.weapon") == 15 then
                charges = charges + 1
                chargeAt = chargeAt or vector(read(n, "r.currentOrigin"))
                -- g_weapon.c:2227 sets effect1Time when pliers finish arming.
                if (read(n, "s.effect1Time") or 0) > 0 then armed = armed + 1 end
            end
            local target, script = read(n, "targetname"), read(n, "scriptName")
            for name in pairs(watched) do
                if name == target or name == script then
                    local previous = entities[name]
                    seen[name] = true
                    if previous.count == 0 then
                        local progress = read(n, "s.angles2") or {}
                        entities[name] = {present = true, seen = true, count = 1, id = n,
                            health = read(n, "health"), state = read(n, "entstate"),
                            classname = read(n, "classname"), position = vector(read(n, "r.currentOrigin")),
                            progress = progress[1], built = progress[2] == 1}
                    else
                        -- A shared target name is ambiguous, never select an arbitrary entity.
                        entities[name] = {present = true, seen = true, count = previous.count + 1}
                    end
                end
            end
        end
    end
    local team, role = read(0, "sess.sessionTeam"), read(0, "sess.playerType")
    local health, mode = read(0, "health"), read(0, "ps.pm_type")
    local height = read(0, "ps.viewheight")
    local velocity = vector(read(0, "ps.velocity"))
    -- PW_REDFLAG 5 / PW_BLUEFLAG 6 (bg_public.h:785-786): a carried objective item.
    local carrying = (read(0, "ps.powerups", 5) or 0) > 0 or (read(0, "ps.powerups", 6) or 0) > 0
    local winner = et.trap_GetConfigstring(et.CS_MULTI_MAPWINNER):match("\\w\\([%-0-9]+)")
    local packet = {time_ms = time, ack = ack, player = {
        connected = read(0, "pers.connected") == 2,
        team = teams[team], role = roles[role], health = health,
        alive = type(health) == "number" and health > 0 and mode == 0,
        view_height = height, carrying = carrying,
        stance = height == 40 and "stand" or height == 16 and "crouch" or height == -8 and "prone" or "unknown",
        weapon = weapons[read(0, "ps.weapon")] or "other", weapon_id = read(0, "ps.weapon"),
        speed = velocity and math.sqrt(velocity[1]^2 + velocity[2]^2 + velocity[3]^2) or nil,
        position = vector(read(0, "ps.origin")), angles = vector(read(0, "ps.viewangles"))
    }, game = {
        phase = phases[tonumber(et.trap_Cvar_Get("gamestate"))] or "unknown",
        winner = winner == "0" and "red" or winner == "1" and "blue" or "none",
        charge_count = charges, armed_charges = armed, charge_position = chargeAt
    }, entities = entities}
    et.G_Print("NRC_PLAYTEST " .. token .. " " .. json(packet) .. "\n")
end
function et_RunFrame(time)
    if time >= nextSample then
        sample(time)
        nextSample = time + 100
    end
end
function et_ClientCommand(client, command)
    if command ~= "nrc_playtest_sync" or client ~= 0 then return 0 end
    -- Acknowledge command ordering without changing game state.
    ack = tonumber(et.trap_Argv(1)) or ack
    return 1
end
