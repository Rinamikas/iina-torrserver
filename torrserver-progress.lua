-- TorrServer playlist, audio preference and playback progress for IINA/Lampa.
local mp = require('mp')
local utils = require('mp.utils')
local msg = require('mp.msg')
local options = {servers = '', audio_memory = '~~/torrserver-audio.json',
    playlist = true, audio_memory_enabled = true, progress = true, server_cache = false,
    companion_url = '', companion_token = ''}
require('mp.options').read_options(options, 'torrserver-progress')
local memory_path = mp.command_native({'expand-path', options.audio_memory})
local allowed_servers = {}
for server in options.servers:gmatch('[^,]+') do
    server = server:gsub('^%s+', ''):gsub('%s+$', ''):gsub('/+$', ''):lower()
    allowed_servers[server] = true
end
local session
local companion = options.companion_url:gsub('/+$', '')
local companion_enabled = options.server_cache and companion:match('^https?://[^/?#]+$')
    and #options.companion_token >= 32 and not options.companion_token:find('[\r\n]')
local client = tostring(utils.getpid()) .. '-' .. tostring(os.time()) .. '-' .. tostring(mp.get_time())
local skip_companion_url
local requested_url, opened_using_companion, playback_loaded

local function companion_request(path, payload)
    if not companion_enabled then return end
    local args = {'/usr/bin/curl', '--noproxy', '*', '--silent', '--show-error',
        '--fail', '--connect-timeout', '1', '--max-time', '2', '--max-filesize', '2097152',
        '--header', 'Authorization: Bearer ' .. options.companion_token}
    if payload then
        args[#args + 1] = '--header'; args[#args + 1] = 'Content-Type: application/json'
        args[#args + 1] = '--data-binary'; args[#args + 1] = utils.format_json(payload)
    end
    args[#args + 1] = companion .. path
    local result = mp.command_native({name = 'subprocess', playback_only = false,
        capture_stdout = true, capture_stderr = true, args = args})
    if result and result.status == 0 then return utils.parse_json(result.stdout or '') end
end

local function notify(event, current)
    current = current or session
    if not current then return end
    return companion_request('/event', {event = event, hash = current.hash,
        index = current.index, client = client, position = current.time or 0,
        duration = current.duration or 0,
        sync = options.progress and current.time and current.time > 0
            and current.saved ~= current.time or false})
end

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
    if not options.audio_memory_enabled then return end
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
    if not options.audio_memory_enabled then return end
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
    if not options.playlist then return end
    -- Preserve a playlist that the user already assembled.
    if mp.get_property_number('playlist-count', 0) ~= 1 then return end
    -- IINA reads the playlist on file-loaded, so finish before that event.
    hook:defer()
    local catalog = companion_request('/catalog/' .. current.hash)
    local command = {
        name = 'subprocess', playback_only = false,
        capture_stdout = true, capture_stderr = true,
        args = {'/usr/bin/curl', '--noproxy', '*', '--silent', '--show-error',
            '--fail', '--connect-timeout', '1', '--max-time', '5',
            '--max-filesize', '1048576',
            current.origin .. '/stream?link=' .. current.hash .. '&stat'}
    }
    local function populated(success, result)
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
            local filename = file.path:match('[^/\\]+$') or file.path
            -- M3U titles are available before an episode has ever been opened.
            local title = filename:gsub('[\r\n]', ' ')
            if file.id ~= current.index then
                local url = current.origin .. '/stream/' .. url_encode(filename)
                    .. '?link=' .. current.hash .. '&index=' .. file.id .. '&play'
                mp.commandv('loadlist', 'memory://#EXTM3U\n#EXTINF:-1,' .. title
                    .. '\n' .. url .. '\n', 'append')
                -- Insert before/after the playing entry without reloading it.
                mp.commandv('playlist-move', mp.get_property_number('playlist-count') - 1, position - 1)
            else
                mp.set_property('file-local-options/force-media-title', title)
            end
        end
        msg.info('TorrServer episodes added to the IINA playlist')
        hook:cont()
    end
    if catalog then
        populated(true, {status = 0, stdout = utils.format_json(catalog)})
    else
        mp.command_native_async(command, populated)
    end
end

local function sample()
    if not session then return end
    local time = mp.get_property_number('time-pos')
    local duration = mp.get_property_number('duration')
    if time and time >= 0 then session.time = time end
    if duration and duration > 0 then session.duration = duration end
end

local function save()
    if not options.progress then return end
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
    local path = mp.get_property('path')
    local current = identify(path)
    requested_url, opened_using_companion, playback_loaded = path, false, false
    if current then
        if options.audio_memory_enabled then mp.set_property('aid', 'auto') end
        if path == skip_companion_url then
            skip_companion_url = nil
            mp.set_property('stream-open-filename', path)
            mp.set_property('file-local-options/http-header-fields', '')
        elseif companion_enabled and notify('start', current) then
            opened_using_companion = true
            -- Keep the original TorrServer URL as mpv's path, preserving playlist/progress identity.
            mp.set_property('stream-open-filename', companion .. '/media/' .. current.hash
                .. '/' .. current.index .. '/video')
            mp.set_property('file-local-options/http-header-fields',
                'Authorization: Bearer ' .. options.companion_token)
        end
    end
end)
mp.add_hook('on_preloaded', 30, function(hook)
    session = identify(mp.get_property('path'))
    if session then populate_playlist(session, hook) end
end)
mp.register_event('file-loaded', function()
    playback_loaded = true
    session = identify(mp.get_property('path'))
    if session then
        if options.progress then msg.info('TorrServer progress tracking enabled') end
        restore_audio()
        sample(); notify('heartbeat')
    end
end)
mp.observe_property('aid', 'string', remember_audio)
mp.observe_property('time-pos', 'number', sample)
mp.observe_property('pause', 'bool', function(_, paused)
    if paused then sample(); save(); notify('heartbeat') end
end)
mp.add_periodic_timer(15, function() sample(); save(); notify('heartbeat') end)
mp.register_event('end-file', function(event)
    remember_audio()
    if session and event.reason == 'eof' and session.duration then
        session.time = session.duration
    end
    save()
    notify('end')
    local failed = event.reason == 'error' and opened_using_companion and not playback_loaded
    local original = failed and requested_url
    session = nil
    if original then
        skip_companion_url = original
        mp.add_timeout(0, function() mp.commandv('loadfile', original, 'replace') end)
    end
end)
mp.register_event('shutdown', function() save(); notify('end') end)
