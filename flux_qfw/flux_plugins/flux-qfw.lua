-- Flux job shell plugin for QFw hybrid jobs.
--
-- The CLI plugin puts the device request into the job's resources and the
-- workload envelope into attributes.system.qfw. By the time this runs,
-- Fluxion has already decided who holds the device. This plugin never
-- acquires a device. It only passes on what the job was given.
--
-- What shell.init does depends on the job.
--
--   scout job            Sets QFW_QPU_SERVICE_ID. Its program does the work.
--   job scope            Exports the job's hosts and the launch agent.
--                        No QPM is involved.
--   --qfw-no-reserve     Sets QFW_QPU_SERVICE_ID and nothing else.
--   user's half of a     Reads the reservation the scout memoed onto the job
--   scout pair           and exports it. The scout closes the session later.
--   site scope           Rank 0 registers the job with the site QPM, memos
--                        the reservation, and exports it. The other ranks
--                        read the memo after the init barrier. At exit
--                        rank 0 unregisters the job.
--
-- Only site scope talks to a QPM. The job already holds the device, so this
-- is bookkeeping. The QPM hands back the reservation id QFw needs.
--
-- QPM calls go through the flux-qfw helper, so the DEFw client runs in
-- Python and not in Lua.

local reservations = nil   -- the QFW_RESERVATIONS value, rank 0 only
local service_id = nil
local reservation_id = nil
local helper_cmd = nil
local backend = nil

local function log(fmt, ...)
    local msg = string.format(fmt, ...)
    if shell and shell.log then
        shell.log("flux-qfw: " .. msg)
    else
        io.stderr:write("flux-qfw: " .. msg .. "\n")
    end
end

local function qfw_attrs()
    local ok, info = pcall(function() return shell.info end)
    if not ok or not info or not info.jobspec then
        return nil
    end
    local attributes = info.jobspec.attributes
    local system = attributes and attributes.system
    return system and system.qfw or nil
end

local function shell_quote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- The helper runs with the environment the user submitted from, which is
-- stored in the jobspec. That is where QFW_SITE_CONFIG and any FLUX_QFW
-- settings live, not in the broker's environment.
local function job_env_prefix()
    local ok, env = pcall(function()
        return shell.info.jobspec.attributes.system.environment
    end)
    if not ok or type(env) ~= "table" then
        return ""
    end
    local parts = {"env"}
    for key, value in pairs(env) do
        if type(key) == "string" and key:match("^[A-Za-z_][A-Za-z0-9_]*$") then
            table.insert(parts, key .. "=" .. shell_quote(value))
        end
    end
    return table.concat(parts, " ") .. " "
end

local function helper(qfw)
    local python = qfw.python or "python3"
    local prefix = job_env_prefix()
    if qfw.pythonpath and qfw.pythonpath ~= "" then
        prefix = prefix .. "PYTHONPATH=" .. shell_quote(qfw.pythonpath) .. " "
    end
    return prefix .. shell_quote(python) .. " -m flux_qfw.cli"
end

-- Run a command and return its last line of output, or nil if it failed.
local function read_last_line(cmd)
    local pipe = io.popen(cmd)
    if not pipe then return nil end
    local output = pipe:read("*a") or ""
    local ok = pipe:close()
    if not ok then return nil end
    local last
    for line in output:gmatch("[^\n]+") do
        if line:match("%S") then last = line end
    end
    return last
end

-- Turn the envelope back into JSON for the helper. shell.info gives us Lua
-- tables, so we write out the few fields the QPM needs.
local function envelope_json(qfw)
    local parts = {}
    local function add(key, value)
        if value == nil then return end
        if type(value) == "number" then
            table.insert(parts, string.format('"%s":%d', key, value))
        elseif type(value) == "boolean" then
            table.insert(parts, string.format('"%s":%s', key, tostring(value)))
        else
            table.insert(parts, string.format('"%s":"%s"', key,
                tostring(value):gsub('"', '\\"')))
        end
    end
    for _, key in ipairs({"qpu", "workload_kind", "circuit_count", "max_qubits",
                          "max_depth", "max_shots", "one_q_gates", "two_q_gates",
                          "measurements", "qpus"}) do
        add(key, qfw[key])
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

-- The job's hosts, one per name and separated by commas, which is the form
-- QFW_SIMULATOR_NODES takes.
local function job_hostlist()
    local ok, nodelist = pcall(function()
        return shell.info.R.execution.nodelist
    end)
    if not ok or type(nodelist) ~= "table" then return nil end
    local parts = {}
    for _, hl in ipairs(nodelist) do table.insert(parts, tostring(hl)) end
    return read_last_line("flux hostlist -e -d , " .. shell_quote(table.concat(parts, ",")))
end

-- The job's id as text. shell.info.jobid is a Lua number, a double, so
-- tostring turns a large id into something like 1.66e+14 and loses digits.
-- The shell puts the exact id in FLUX_JOB_ID, so we use that.
local function job_id()
    local id = shell.getenv("FLUX_JOB_ID")
    if id and id ~= "" then
        return id
    end
    return string.format("%.0f", shell.info.jobid)
end

local function export(value, sid)
    shell.setenv("QFW_RESERVATIONS", value)
    shell.setenv("QFW_QPU_SERVICE_ID", sid)
end

plugin.register {
    name = "flux-qfw",
    handlers = {
        {
            topic = "shell.init",
            fn = function()
                local qfw = qfw_attrs()
                if not qfw then return end
                service_id = qfw.qpu
                backend = qfw.backend
                helper_cmd = helper(qfw)
                if qfw.pair == "scout" then
                    -- The scout job holds the device. Its program opens the
                    -- vendor session and serves the held job.
                    shell.setenv("QFW_QPU_SERVICE_ID", service_id)
                    return
                end
                if qfw.scope == "job" then
                    -- The job runs the simulator itself on its own nodes.
                    -- Every rank gets the job's hosts and the launch agent,
                    -- so the MPI runtime starts its daemons with flux exec.
                    local hosts = job_hostlist()
                    shell.setenv("QFW_QPU_SERVICE_ID", service_id)
                    shell.setenv("QFW_QPU_SCOPE", "job")
                    if hosts then shell.setenv("QFW_SIMULATOR_NODES", hosts) end
                    if qfw.launch_agent and qfw.launch_agent ~= "" then
                        shell.setenv("PRTE_MCA_plm_ssh_agent", qfw.launch_agent)
                        shell.setenv("PRTE_MCA_plm_ssh_no_tree_spawn", "1")
                    end
                    if shell.info.rank == 0 then
                        log("job-scoped device %s: simulator hosts %s, no site reservation",
                            tostring(service_id), tostring(hosts))
                    end
                    return
                end
                if qfw.reserve == false then
                    log("device %s scheduled, the QPM is not told (--qfw-no-reserve)",
                        tostring(service_id))
                    shell.setenv("QFW_QPU_SERVICE_ID", service_id)
                    return
                end
                if shell.info.rank ~= 0 then
                    return  -- the other ranks read the memo in shell.post-init
                end
                local jobid = job_id()
                if qfw.hold then
                    -- The user's half of a scout pair. The scout memoed the
                    -- reservation before it released this job, and it keeps
                    -- the device and the session until this job ends. We
                    -- only export.
                    local line = read_last_line(string.format(
                        "%s memo --jobid %s --timeout 30", helper_cmd, jobid))
                    if not line then
                        error(string.format(
                            "flux-qfw: job %s was released without a session reservation "
                            .. "memo; was it released by hand instead of by its scout?",
                            jobid))
                    end
                    reservations = line
                    export(line, service_id)
                    log("job %s uses the session reservation %s on %s its scout obtained; "
                        .. "QFW_RESERVATIONS exported",
                        jobid, tostring(line:match('%["[^"]+","(%d+)"%]')), tostring(service_id))
                    return
                end
                -- Site scope. Tell the QPM this job holds its device.
                local duration = 0
                local sys = shell.info.jobspec.attributes.system
                if sys and sys.duration then duration = math.floor(sys.duration) end
                local uid = read_last_line("id -u") or "0"
                local user = read_last_line("id -un") or "unknown"
                local cmd = string.format(
                    "%s register --jobid %s --userid %s --username %s --duration %d "
                    .. "--envelope %s%s",
                    helper_cmd, jobid, uid, shell_quote(user), duration,
                    shell_quote(envelope_json(qfw)),
                    backend and (" --backend " .. shell_quote(backend)) or "")
                local line = read_last_line(cmd)
                if not line then
                    error(string.format(
                        "flux-qfw: could not register job %s with QPM %s; "
                        .. "see the flux-qfw error above",
                        jobid, tostring(service_id)))
                end
                reservations = line
                reservation_id = line:match('%["[^"]+","(%d+)"%]')
                -- Memo the reservation so the other ranks can read it from
                -- the eventlog. No shared filesystem is needed.
                os.execute(string.format(
                    "flux job memo %s qfw_reservations=%s >/dev/null 2>&1",
                    jobid, shell_quote(line)))
                export(line, service_id)
                log("job %s registered with QPM %s (reservation %s), QFW_RESERVATIONS exported",
                    jobid, tostring(service_id), tostring(reservation_id))
            end,
        },
        {
            topic = "shell.post-init",
            fn = function()
                local qfw = qfw_attrs()
                if not qfw or qfw.pair == "scout" or qfw.scope == "job" or qfw.reserve == false
                    or shell.info.rank == 0 then
                    return
                end
                -- Rank 0 posted the memo before the init barrier.
                local jobid = job_id()
                local line = read_last_line(string.format(
                    "%s memo --jobid %s", helper(qfw), jobid))
                if not line then
                    error("flux-qfw: rank 0 posted no reservation memo for job " .. jobid)
                end
                export(line, qfw.qpu)
                log("rank %d exported QFW_RESERVATIONS from the job memo", shell.info.rank)
            end,
        },
        {
            topic = "shell.exit",
            fn = function()
                -- Only a site scope job registered, so only it unregisters.
                if not reservation_id or not service_id then return end
                local cmd = string.format(
                    "%s unregister --service %s --reservation-id %s%s",
                    helper_cmd, shell_quote(service_id), reservation_id,
                    backend and (" --backend " .. shell_quote(backend)) or "")
                local line = read_last_line(cmd)
                if not line then
                    log("could not unregister reservation %s on %s; the QPM will expire it",
                        reservation_id, tostring(service_id))
                else
                    log("unregistered reservation %s on %s", reservation_id, tostring(service_id))
                end
            end,
        },
    },
}
