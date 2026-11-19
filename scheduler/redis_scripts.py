"""Atomic scheduler state changes. All keys belong to a single Redis instance.

Pending state has no TTL (and is protected by volatile-lru); terminal records
expire. Deadlines and processing leases are swept by every live gateway. A
completion is fenced by its lease token. Recovery can repeat an external model
call whose outcome is unknown: this is not exactly-once inference.
"""

RELIABLE_SCRIPT = r'''
local prefix, op = ARGV[1], ARGV[2]
local arg, cfg = cjson.decode(ARGV[3]), cjson.decode(ARGV[4])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local function jk(id) return prefix .. 'job:' .. id end
local function wk(id) return prefix .. 'waiters:' .. id end
local function waitkey(id)
    local key = wk(id)
    local kind = redis.call('TYPE', key).ok
    if kind == 'list' then
        local legacy = redis.call('LRANGE', key, 0, -1)
        redis.call('DEL', key)
        for _, raw in ipairs(legacy) do
            local request = cjson.decode(raw)
            redis.call('HSET', key, request.request_id, raw)
        end
    end
    return key
end
local function release(id, user)
    redis.call('SREM', prefix .. 'active', id)
    if user then redis.call('SREM', prefix .. 'active:' .. user, id) end
    redis.call('ZREM', prefix .. 'deadlines', id)
end
local function put(user, request)
    if redis.call('RPUSH', prefix .. 'queue:' .. user, cjson.encode(request)) == 1 then
        redis.call('RPUSH', prefix .. 'rotation', user)
    end
    redis.call('INCR', prefix .. 'pending')
end
local function deliver(request, message)
    local id, key = request.request_id, jk(request.request_id)
    if redis.call('HGET', key, 'status') == 'CANCELLED' then return end
    local encoded = cjson.encode(message)
    redis.call('HSET', key, 'status', message.error and 'FAILED' or 'COMPLETED',
               'user_id', request.user_id, 'result', encoded)
    redis.call('EXPIRE', key, cfg.retention)
    release(id, request.user_id)
    local target = prefix .. 'results:' .. request.reply_to
    redis.call('RPUSH', target, encoded)
    redis.call('EXPIRE', target, cfg.retention)
end
local function finish(request, message, token)
    local id, key = request.request_id, jk(request.request_id)
    local status = redis.call('HGET', key, 'status')
    if status == 'COMPLETED' or status == 'FAILED' or status == 'CANCELLED' then return 0 end
    if token and redis.call('HGET', key, 'token') ~= token then return 0 end
    if redis.call('HGET', key, 'caller_done') == '1' then
        -- The leader's own caller already received its timeout; only close the shared job.
        redis.call('HSET', key, 'status', 'FAILED')
        redis.call('EXPIRE', key, cfg.retention)
        release(id, request.user_id)
    else
        deliver(request, message)
    end
    local waiting = redis.call('HVALS', waitkey(id))
    for _, raw in ipairs(waiting) do
        local follower = cjson.decode(raw)
        local reply = cjson.decode(cjson.encode(message))
        reply.request_id = follower.request_id
        if reply.response then
            reply.response.request_id = follower.request_id
            reply.response.user_id = follower.user_id
            reply.response.coalesced = true
        end
        deliver(follower, reply)
    end
    redis.call('DEL', wk(id))
    if request.dedup_key and redis.call('GET', prefix .. 'inflight:' .. request.dedup_key) == id then
        redis.call('DEL', prefix .. 'inflight:' .. request.dedup_key)
    end
    redis.call('ZREM', prefix .. 'processing', id)
    redis.call('HDEL', key, 'token')
    return 1
end
local function failure(request, kind, text)
    return finish(request, {request_id=request.request_id, error=text, error_type=kind}, nil)
end
-- A leader's deadline belongs to its own caller. When coalesced followers that joined later
-- still have time left, time out only the leader's caller and keep the shared call running
-- until the latest follower deadline. Returns true when the shared job continues.
local function expire_leader(id, r)
    local key = jk(id)
    if redis.call('HGET', key, 'leader') then return false end
    local latest = 0
    for _, raw in ipairs(redis.call('HVALS', waitkey(id))) do
        local d = tonumber(cjson.decode(raw).deadline or 0) or 0
        if d > latest then latest = d end
    end
    if latest <= now then return false end
    if redis.call('HGET', key, 'caller_done') ~= '1' then
        local encoded = cjson.encode({request_id=id, error='Request deadline exceeded',
                                      error_type='SchedulerTimeoutError'})
        redis.call('HSET', key, 'result', encoded, 'caller_done', '1', 'detached', '1')
        local target = prefix .. 'results:' .. r.reply_to
        redis.call('RPUSH', target, encoded)
        redis.call('EXPIRE', target, cfg.retention)
        if r.user_id then redis.call('SREM', prefix .. 'active:' .. r.user_id, id) end
    end
    r.deadline = latest
    redis.call('HSET', key, 'request', cjson.encode(r))
    redis.call('ZADD', prefix .. 'deadlines', latest, id)
    return true
end
local function cancel_one(id)
    local key = jk(id)
    local raw = redis.call('HGET', key, 'request')
    local request = raw and cjson.decode(raw) or nil
    redis.call('HSET', key, 'status', 'CANCELLED')
    redis.call('HDEL', key, 'token')
    redis.call('EXPIRE', key, cfg.retention)
    release(id, redis.call('HGET', key, 'user_id'))
    redis.call('ZREM', prefix .. 'processing', id)
    if request and request.dedup_key and redis.call('GET', prefix .. 'inflight:' .. request.dedup_key) == id then
        redis.call('DEL', prefix .. 'inflight:' .. request.dedup_key)
    end
end

if op == 'submit' then
    local r, id = arg.request, arg.request.request_id
    if redis.call('EXISTS', jk(id)) == 1 then return redis.call('HGET', jk(id), 'leader') or '' end
    if redis.call('SCARD', prefix .. 'active') >= cfg.max_pending or
       tonumber(redis.call('GET', prefix .. 'pending') or '0') >= cfg.max_pending or
       redis.call('LLEN', prefix .. 'queue:' .. r.user_id) >= cfg.max_per_user or
       redis.call('SCARD', prefix .. 'active:' .. r.user_id) >= cfg.max_per_user then return '!full' end
    r.deadline = now + cfg.timeout
    redis.call('HSET', jk(id), 'status', 'PENDING', 'user_id', r.user_id,
               'request', cjson.encode(r), 'attempts', 0)
    redis.call('SADD', prefix .. 'active', id)
    redis.call('SADD', prefix .. 'active:' .. r.user_id, id)
    redis.call('ZADD', prefix .. 'deadlines', r.deadline, id)
    if r.dedup_key then
        local inflight = prefix .. 'inflight:' .. r.dedup_key
        local leader = redis.call('GET', inflight)
        local status = leader and redis.call('HGET', jk(leader), 'status') or nil
        if leader and (status == 'PENDING' or status == 'PROCESSING') then
            redis.call('HSET', jk(id), 'leader', leader)
            redis.call('HSET', waitkey(leader), id, cjson.encode(r))
            -- A queued shared call may run at the earliest participant's fair turn.
            if status == 'PENDING' then
                r.leader_id = leader
                put(r.user_id, r)
            end
            return leader
        end
        redis.call('SET', inflight, id)
    end
    put(r.user_id, r)
    return ''
elseif op == 'take' then
    for n=1,32 do
        local user = redis.call('LPOP', prefix .. 'rotation')
        if not user then return nil end
        local queue = prefix .. 'queue:' .. user
        local raw = redis.call('LPOP', queue)
        if redis.call('LLEN', queue) > 0 then redis.call('RPUSH', prefix .. 'rotation', user) end
        if raw then
            redis.call('DECR', prefix .. 'pending')
            local marker = cjson.decode(raw)
            local id = marker.leader_id or marker.request_id
            local key = jk(id)
            local saved = redis.call('HGET', key, 'request')
            local r = saved and cjson.decode(saved) or marker
            local status = redis.call('HGET', key, 'status')
            if marker.leader_id and redis.call('HGET', jk(marker.request_id), 'status') ~= 'PENDING' then
                status = 'CANCELLED'
            end
            if not status then
                -- The queue contains enough metadata to send a terminal error.
                r.user_id = r.user_id or user
                failure(r, 'SchedulerUnavailableError', 'Queued job state was lost')
            elseif status == 'PENDING' then
                -- Old queued jobs predate request metadata/deadlines. Preserve them.
                r.user_id = r.user_id or user
                r.deadline = r.deadline or (now + cfg.timeout)
                redis.call('HSET', key, 'request', cjson.encode(r))
                redis.call('PERSIST', key)
                if r.deadline <= now and not expire_leader(id, r) then
                    failure(r, 'SchedulerTimeoutError', 'Request deadline exceeded')
                else
                    local attempts = redis.call('HINCRBY', key, 'attempts', 1)
                    redis.call('HSET', key, 'status', 'PROCESSING', 'token', arg.token)
                    redis.call('ZADD', prefix .. 'processing', now + cfg.lease, id)
                    redis.call('ZADD', prefix .. 'deadlines', r.deadline, id)
                    r.lease_token = arg.token
                    return {user, cjson.encode(r)}
                end
            end
        end
    end
    return nil
elseif op == 'complete' then
    -- The stored deadline may have been extended for followers after this job was taken.
    local saved = redis.call('HGET', jk(arg.request.request_id), 'request')
    local stored = saved and cjson.decode(saved) or arg.request
    if (stored.deadline or arg.request.deadline) <= now
       and not expire_leader(arg.request.request_id, stored) then
        return finish(arg.request, {request_id=arg.request.request_id,
            error='Request deadline exceeded', error_type='SchedulerTimeoutError'}, arg.request.lease_token)
    end
    return finish(arg.request, arg.message, arg.request.lease_token)
elseif op == 'renew' then
    if redis.call('HGET', jk(arg.id), 'token') ~= arg.token then return 0 end
    redis.call('ZADD', prefix .. 'processing', now + cfg.lease, arg.id)
    return 1
elseif op == 'maintain' then
    -- Cancelled/finished queue markers must not consume capacity indefinitely.
    local users = redis.call('LRANGE', prefix .. 'rotation', 0, 63)
    for _, user in ipairs(users) do
        local queue = prefix .. 'queue:' .. user
        for n=1,32 do
            local raw = redis.call('LINDEX', queue, 0)
            if not raw then break end
            local marker = cjson.decode(raw)
            local status = redis.call('HGET', jk(marker.request_id), 'status')
            local leader_status = marker.leader_id and redis.call('HGET', jk(marker.leader_id), 'status') or nil
            if status == 'PENDING' and (not marker.leader_id or leader_status == 'PENDING') then break end
            -- Missing metadata must pass through take(), which sends an error.
            if not status then break end
            redis.call('LPOP', queue)
            redis.call('DECR', prefix .. 'pending')
        end
        if redis.call('LLEN', queue) == 0 then redis.call('LREM', prefix .. 'rotation', 0, user) end
    end
    local expired = redis.call('ZRANGEBYSCORE', prefix .. 'deadlines', '-inf', now, 'LIMIT', 0, 100)
    for _, id in ipairs(expired) do
        local raw = redis.call('HGET', jk(id), 'request')
        if raw then
            local r = cjson.decode(raw)
            local leader = redis.call('HGET', jk(id), 'leader')
            if leader then
                redis.call('HDEL', waitkey(leader), id)
                failure(r, 'SchedulerTimeoutError', 'Request deadline exceeded')
            elseif not expire_leader(id, r) then
                failure(r, 'SchedulerTimeoutError', 'Request deadline exceeded')
            end
        else redis.call('ZREM', prefix .. 'deadlines', id) end
    end
    local stale = redis.call('ZRANGEBYSCORE', prefix .. 'processing', '-inf', now, 'LIMIT', 0, 100)
    for _, id in ipairs(stale) do
        local key = jk(id)
        local raw = redis.call('HGET', key, 'request')
        if raw and redis.call('HGET', key, 'status') == 'PROCESSING' then
            local r = cjson.decode(raw)
            if tonumber(redis.call('HGET', key, 'attempts') or '0') >= cfg.attempts then
                failure(r, 'SchedulerUnavailableError', 'Processing lease expired; recovery limit reached')
            else
                redis.call('HSET', key, 'status', 'PENDING')
                redis.call('HDEL', key, 'token')
                put(r.user_id, r)
            end
        end
        redis.call('ZREM', prefix .. 'processing', id)
    end
    return #expired + #stale
elseif op == 'cancel' then
    local id, key = arg.id, jk(arg.id)
    local status = redis.call('HGET', key, 'status')
    if not status or status == 'COMPLETED' or status == 'FAILED' or status == 'CANCELLED' then return 0 end
    local leader = redis.call('HGET', key, 'leader')
    if leader then
        redis.call('HDEL', waitkey(leader), id)
        cancel_one(id)
        if redis.call('HLEN', waitkey(leader)) == 0 and redis.call('HGET', jk(leader), 'detached') == '1' then
            cancel_one(leader)
            redis.call('DEL', wk(leader))
        end
        return 1
    end
    if redis.call('HLEN', waitkey(id)) > 0 then
        redis.call('HSET', key, 'detached', '1')
        return 0
    end
    cancel_one(id)
    return 1
end
return redis.error_reply('unknown scheduler operation')
'''
