-- TorrServer playlist, audio preference and playback progress for IINA/Lampa.
local mp = require('mp')
local utils = require('mp.utils')
local msg = require('mp.msg')
local options = {servers = '', audio_memory = '~~/torrserver-audio.json'}
require('mp.options').read_options(options, 'torrserver-progress')
local memory_path = mp.command_native({'expand-path', options.audio_memory})
local allowed_servers = {}
for server in options.servers:gmatch('[^,]+') do
    server = server:gsub('^%s+', ''):gsub('%s+$', ''):gsub('/+$', ''):lower()
    allowed_servers[server] = true
end
local session

local function identify(url)
    if not url then return end
    local origin, path = url:match('^(https?://[^/]+)(/.*)$')
    if not origin then return end
    -- Never send playback information to a host supplied by an arbitrary video.
    if not allowed_servers[origin:lower()] then return end
    local hash, index
    if path:match('^/stream[%/?]') then
        hash = path:match('[?&]link=([%x]+)[&]') or path:match('[?&]link=([%x]+)$')
        index = path:match('[?&]index=(%d+)[&]') or path:match('[?&]index=(%d+)$')
    else
        hash, index = path:match('^/play/([%x]+)/(%d+)')
    end
    if not hash or #hash ~= 40 or not index or tonumber(index) < 1 then return end
    return {origin = origin, hash = hash:lower(), index = tonumber(index), time = 0}
end

local function read_memory()
    local file = io.open(memory_path, 'r')
    if not file then return {} end
    local data = file:read('*a')
    file:close()
    local memory = utils.parse_json(data)
    if type(memory) == 'table' then return memory end
    msg.warn('Audio memory is invalid; leaving the file untouched')
end

local function normalize(value)
    return (value or ''):lower():gsub('^%s+', ''):gsub('%s+$', ''):gsub('%s+', ' ')
end

local function audio_description(track)
    local lang = normalize(track.lang)
    lang = ({rus = 'ru', eng = 'en', jpn = 'ja'})[lang] or lang
    return {title = normalize(track.title), lang = lang,
        codec = track.codec or '', channels = track['demux-channel-count'] or 0}
end

local function selected_audio()
    local aid = mp.get_property_number('aid')
    for _, track in ipairs(mp.get_property_native('track-list', {})) do
        if track.type == 'audio' and track.id == aid then return track end
    end
end

local function remember_audio()
    if not session or not session.audio_ready then return end
    local track = selected_audio()
    if not track or session.last_aid == track.id then return end
    session.last_aid = track.id
    local description = audio_description(track)
    -- A track number alone cannot identify the same voice in another episode.
    if description.title == '' and description.lang == '' then return end
    local memory = read_memory()
    if not memory then return end
    memory[session.hash] = description
    local encoded = utils.format_json(memory)
    if not encoded then msg.warn('Could not encode audio memory'); return end
    local file = io.open(memory_path .. '.tmp', 'w')
    if not file then msg.warn('Could not write audio memory'); return end
    local ok = file:write(encoded)
    local closed = file:close()
    if not ok or not closed or not os.rename(memory_path .. '.tmp', memory_path) then
        os.remove(memory_path .. '.tmp')
        msg.warn('Could not save audio memory')
    else
        msg.info('Audio preference saved for this torrent')
    end
end

local function restore_audio()
    local memory = read_memory()
    local wanted = memory and memory[session.hash]
    if type(wanted) == 'table' and type(wanted.title) == 'string'
        and type(wanted.lang) == 'string' then
        local matches, exact = {}, {}
        for _, track in ipairs(mp.get_property_native('track-list', {})) do
            if track.type == 'audio' then
                local description = audio_description(track)
                if description.title == wanted.title and description.lang == wanted.lang
                    and (wanted.title ~= '' or wanted.lang ~= '') then
                    matches[#matches + 1] = track.id
                    if description.codec == wanted.codec and description.channels == wanted.channels then
                        exact[#exact + 1] = track.id
                    end
                end
            end
        end
        local aid = #matches == 1 and matches[1] or (#exact == 1 and exact[1])
        if aid then
            mp.set_property_number('aid', aid)
            msg.info('Audio preference restored for this torrent')
        else
            msg.warn('Saved audio track is absent or ambiguous; keeping the current track')
        end
    end
    session.last_aid = mp.get_property_number('aid')
    session.audio_ready = true
end

local video_extensions = {mkv=true, mp4=true, avi=true, mov=true, m4v=true,
    ts=true, m2ts=true, mts=true, mpg=true, mpeg=true, webm=true, wmv=true,
    flv=true, ogv=true, vob=true}

local function natural_key(path)
    return path:lower():gsub('%d+', function(digits)
        local significant = digits:gsub('^0+', '')
        return string.format('%06d:%s', #significant, significant)
    end)
end

local function url_encode(value)
    return (value:gsub('[^%w%.%-%_~]', function(byte)
        return string.format('%%%02X', byte:byte())
    end))
end

local function populate_playlist(current, hook)
    -- Preserve a playlist that the user already assembled.
    if mp.get_property_number('playlist-count', 0) ~= 1 then return end
    -- IINA reads the playlist on file-loaded, so finish before that event.
    hook:defer()
    mp.command_native_async({
        name = 'subprocess', playback_only = false,
        capture_stdout = true, capture_stderr = true,
        args = {'/usr/bin/curl', '--noproxy', '*', '--silent', '--show-error',
            '--fail', '--connect-timeout', '1', '--max-time', '5',
            '--max-filesize', '1048576',
            current.origin .. '/stream?link=' .. current.hash .. '&stat'}
    }, function(success, result)
        if session ~= current or mp.get_property_number('playlist-count', 0) ~= 1 then
            hook:cont()
            return
        end
        local detail = success and result and result.status == 0
            and utils.parse_json(result.stdout or '')
        if type(detail) ~= 'table' or detail.hash ~= current.hash
            or type(detail.file_stats) ~= 'table' then
            msg.warn('TorrServer playlist could not be loaded; keeping the current file')
            hook:cont()
            return
        end
        local files, ids = {}, {}
        for _, file in ipairs(detail.file_stats) do
            if type(file.path) == 'string' and type(file.id) == 'number'
                and file.id >= 1 and file.id == math.floor(file.id) then
                local extension = file.path:lower():match('%.([^%.]+)$')
                if video_extensions[extension] and not ids[file.id] then
                    ids[file.id] = true
                    files[#files + 1] = file
                end
            end
        end
        if not ids[current.index] or #files < 2 then hook:cont(); return end
        table.sort(files, function(a, b)
            local ka, kb = natural_key(a.path), natural_key(b.path)
            return ka < kb or (ka == kb and a.id < b.id)
        end)
        for position, file in ipairs(files) do
            if file.id ~= current.index then
                local filename = file.path:match('[^/\\]+$') or file.path
                local url = current.origin .. '/stream/' .. url_encode(filename)
                    .. '?link=' .. current.hash .. '&index=' .. file.id .. '&play'
                mp.commandv('loadfile', url, 'append')
                -- Insert before/after the playing entry without reloading it.
                mp.commandv('playlist-move', mp.get_property_number('playlist-count') - 1, position - 1)
            end
        end
        msg.info('TorrServer episodes added to the IINA playlist')
        hook:cont()
    end)
end

local function sample()
    if not session then return end
    local time = mp.get_property_number('time-pos')
    local duration = mp.get_property_number('duration')
    if time and time >= 0 then session.time = time end
    if duration and duration > 0 then session.duration = duration end
end

local function save()
    if not session or session.time <= 0 or session.saved == session.time then return end
    local result = mp.command_native({
        name = 'subprocess', playback_only = false,
        capture_stdout = true, capture_stderr = true,
        args = {'/usr/bin/curl', '--noproxy', '*', '--silent', '--show-error',
            '--fail', '--connect-timeout', '1', '--max-time', '2',
            '--request', 'POST', '--header', 'Content-Type: application/json',
            '--data-binary', utils.format_json({action = 'set', hash = session.hash,
                file_index = session.index, timecode = session.time}),
            session.origin .. '/viewed'}
    })
    if result and result.status == 0 then
        session.saved = session.time
        msg.info('Playback position saved to TorrServer')
    else
        msg.warn('TorrServer progress could not be saved; will retry at the next update')
    end
end

mp.add_hook('on_load', 20, function()
    -- Do not inherit an unrelated torrent's numeric track ID through mpv.
    if identify(mp.get_property('path')) then mp.set_property('aid', 'auto') end
end)
mp.add_hook('on_preloaded', 30, function(hook)
    session = identify(mp.get_property('path'))
    if session then populate_playlist(session, hook) end
end)
mp.register_event('file-loaded', function()
    session = identify(mp.get_property('path'))
    if session then
        msg.info('TorrServer progress tracking enabled')
        restore_audio()
    end
end)
mp.observe_property('aid', 'string', remember_audio)
mp.observe_property('time-pos', 'number', sample)
mp.observe_property('pause', 'bool', function(_, paused)
    if paused then sample(); save() end
end)
mp.add_periodic_timer(15, function() sample(); save() end)
mp.register_event('end-file', function(event)
    remember_audio()
    if session and event.reason == 'eof' and session.duration then
        session.time = session.duration
    end
    save()
    session = nil
end)
mp.register_event('shutdown', save)
