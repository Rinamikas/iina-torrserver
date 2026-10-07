# IINA TorrServer Companion

[English](#english) · [Русский](#русский)

## English

Watch torrent seasons in IINA. Keep your audio choice and send progress back to Lampa through TorrServer. No Lampa changes, cloud account or playlist export.

### What's included

- **Episodes:** open one video; the torrent's videos appear in IINA's playlist. Real filenames show immediately, in natural order. Existing playlists are preserved.
- **Audio memory:** choose a voice once for the torrent. It follows you between episodes and after restart, even if track numbers change. If no clear match exists, IINA keeps its default.
- **Progress:** saved every 15 seconds, on pause, stop and exit. Lampa shows it after you reopen the torrent's file list.
- **Optional downloads:** save the current episode, one previous episode and up to three next episodes on the server Mac. Resume interrupted downloads and watch while downloading. Fully cached files work without TorrServer; the helper must stay running.
- **Optional cleanup:** keep the previous episode; remove older episodes and files beyond the three-episode lookahead. Remove unused caches after two weeks. Active playback is protected. Matching audio/subtitle files follow their episode; torrent entries and watched history remain.
- **Configuration switches:** every feature can be disabled. **Only IINA improvements are enabled by default. Server downloads and cleanup are off.**

Player improvements use one Lua file. Disk storage adds one optional Python helper, without extra Python packages.

### Install in IINA

Tested on macOS with **IINA 1.3.5 and 1.5.0** and **TorrServer MatriX.145.1**. TorrServer authentication is not supported by this integration.

1. Download **Code → Download ZIP**, extract it and open Terminal in that folder:

   ```sh
   mkdir -p "$HOME/.config/mpv/scripts" "$HOME/.config/mpv/script-opts"
   cp torrserver-progress.lua "$HOME/.config/mpv/scripts/"
   cp script-opts/torrserver-progress.conf "$HOME/.config/mpv/script-opts/"
   ```

   Use IINA's existing configuration folder if you have one. Keep your configuration when updating.

2. Edit `script-opts/torrserver-progress.conf`. `servers` must match the address in playback links, including the port. Separate aliases with commas. Defaults:

   ```ini
   servers=http://torrserver.local:8090
   playlist=yes
   audio_memory_enabled=yes
   progress=yes
   server_cache=no
   ```

   The switches control episodes, audio memory, progress and server cache. Use `yes`/`no`.

3. In **IINA → Settings → Advanced**, enable advanced settings and **Use config directory**. Select that folder. Fully quit and reopen IINA after configuration changes.
4. Enable **TrackTimecode** in TorrServer and Lampa. Select IINA as Lampa's external player. Open an episode and navigate in IINA's playlist.

This integration loads as an **mpv Lua script** through the config directory. It does not appear in IINA's **Plugins** tab, which lists native JavaScript plugins packaged as `.iinaplugin`.

### Optional server downloads

Requires a server Mac, **Python 3.9+** and TorrServer with **`--webdav`** added to its existing startup command. Normal RAM cache settings can stay unchanged.

1. Copy the project to the server Mac. In its folder, replace `SERVER_LAN_IP` with the Mac's private LAN address and run:

   ```sh
   python3 companion.py --install --bind SERVER_LAN_IP
   ```

   The installer creates `~/Library/Application Support/iina-torrserver/companion.json`. The helper starts after reboot and recovers crashes. Downloads and cleanup initially remain off.

2. In that existing JSON file, set `download_enabled`, `cleanup_watched` and `cleanup_expired` to `true` to enable downloading, rolling episode cleanup and two-week cleanup. Each defaults to `false` and works independently. `keep_previous: 1` and `download_ahead: 3` set the episode window; `retention_seconds: 1209600` sets 14 days. Close IINA and repeat the installation command to apply changes; settings and cached files are preserved.
3. In IINA's configuration, set:

   ```ini
   server_cache=yes
   companion_url=http://SERVER_MAC.local:8092
   companion_token=YOUR_PRIVATE_TOKEN
   ```

   Copy `token` from the private server configuration. Keep it private. Restart IINA.

4. Open an episode from Lampa to enroll its torrent. Cached bytes come from disk; missing bytes stream through TorrServer. With `progress=yes`, failed progress updates are stored and retried when TorrServer returns.

Limits when enabled: **64 GiB** cache, **10 GiB** free-space reserve, **14 days** since last playback. Disk limits pause downloads. Storage is `~/Library/Caches/iina-torrserver/`, separate from TorrServer's cache. Keep the authenticated helper on a private LAN.

### Seeking and limits

**TorrServer buffers pieces near your playback position, mainly ahead.** Seeking near the end does not require downloading the whole beginning first.

**Our disk download is sequential:** from the beginning or the saved prefix. Seeking does not move it; missing playback data still comes through TorrServer. The current episode has priority, then up to three next episodes and the retained previous episode. A download that leaves this window stops after its current bounded request; its data is not appended.

Seeking past 95% counts as watched. This mark no longer deletes the current or previous episode; cleanup follows playlist order, even for skipped episodes. Audio memory covers the whole torrent. External audio/subtitles are downloaded but not attached automatically. Direct `/stream` and `/play` links work; `edl://` playlists do not. An unavailable helper falls back to TorrServer before playback starts; a mid-playback failure may interrupt viewing.

To disconnect IINA, set `server_cache=no`. To stop existing background jobs and cleanup too, set all three server switches to `false` and rerun the installer with IINA closed. To remove the player integration, delete its Lua/config files and restart IINA.

### Tests and license

Automated tests cover downloads, playback, cleanup, settings and cache safety. Separate tests use IINA's actual engine and a locally seeded torrent. See [`tests/`](tests).

[MIT](LICENSE): use, modify, share and sell, including in closed-source projects. Keep the license notice. No warranty.

---

## Русский

Смотрите торрент-сериалы в IINA. Сохраняйте озвучку и передавайте прогресс в Lampa через TorrServer. Изменения Lampa, облако и экспорт списка серий не нужны.

### Возможности

- **Список серий:** откройте одно видео — остальные появятся в IINA. Правильные названия видны сразу, порядок естественный. Уже собранный список сохраняется.
- **Память озвучки:** выберите голос один раз для торрента. Он восстановится в других сериях и после перезапуска, даже если номера дорожек изменились. Без однозначного совпадения остаётся выбор IINA.
- **Прогресс:** сохраняется каждые 15 секунд, при паузе, остановке и выходе. Lampa покажет его после повторного открытия списка файлов торрента.
- **Скачивание по желанию:** сохраняйте текущую, одну предыдущую и максимум три следующие серии на серверном Mac. Продолжайте прерванную загрузку и смотрите до её завершения. Полностью скачанные файлы работают без TorrServer; помощник должен быть запущен.
- **Очистка по желанию:** предыдущая серия сохраняется; более ранние и файлы дальше трёх следующих удаляются. Неиспользуемые кэши удаляются через две недели. Активный просмотр защищён. Соответствующие аудио и субтитры удаляются вместе с серией; записи торрентов и история просмотра сохраняются.
- **Переключатели:** каждую функцию можно отключить. **По умолчанию включены только улучшения IINA. Скачивание и очистка выключены.**

Плееру нужен один Lua-файл. Для хранения на диске добавляется необязательный помощник на Python, без дополнительных пакетов.

### Установка в IINA

Проверено на macOS с **IINA 1.3.5 и 1.5.0** и **TorrServer MatriX.145.1**. Авторизация TorrServer в дополнении не поддерживается.

1. Скачайте **Code → Download ZIP**, распакуйте архив и откройте Терминал в его папке:

   ```sh
   mkdir -p "$HOME/.config/mpv/scripts" "$HOME/.config/mpv/script-opts"
   cp torrserver-progress.lua "$HOME/.config/mpv/scripts/"
   cp script-opts/torrserver-progress.conf "$HOME/.config/mpv/script-opts/"
   ```

   Если у IINA уже есть папка конфигурации, используйте её. При обновлении сохраняйте свою конфигурацию.

2. Измените `script-opts/torrserver-progress.conf`. `servers` должен совпадать с адресом в ссылках воспроизведения, включая порт. Несколько адресов разделяются запятыми. По умолчанию:

   ```ini
   servers=http://torrserver.local:8090
   playlist=yes
   audio_memory_enabled=yes
   progress=yes
   server_cache=no
   ```

   Переключатели управляют списком серий, озвучкой, прогрессом и серверным кэшем. Значения — `yes`/`no`.

3. В **IINA → Настройки → Дополнительно** включите расширенные настройки и **Use config directory**. Выберите эту папку. После изменений конфигурации полностью закройте и снова откройте IINA.
4. Включите **TrackTimecode** в TorrServer и Lampa. Выберите IINA внешним плеером Lampa. Откройте серию и переключайтесь через список IINA.

Дополнение загружается как **Lua-скрипт mpv** через папку конфигурации. Во вкладке **«Плагины»** IINA оно не отображается: там перечисляются плагины на JavaScript в пакетах `.iinaplugin`.

### Скачивание на сервере — по желанию

Нужны серверный Mac, **Python 3.9+** и TorrServer с **`--webdav`** в существующей команде запуска. Обычные настройки кэша в памяти можно оставить прежними.

1. Скопируйте проект на серверный Mac. В его папке выполните команду, заменив `SERVER_LAN_IP` на адрес Mac в домашней сети:

   ```sh
   python3 companion.py --install --bind SERVER_LAN_IP
   ```

   Создастся `~/Library/Application Support/iina-torrserver/companion.json`. Помощник запускается после перезагрузки и восстанавливается после сбоев. Скачивание и очистка пока выключены.

2. В этом существующем JSON-файле задайте `true` для `download_enabled`, `cleanup_watched` и `cleanup_expired`: скачивание, очистка по порядку серий и через две недели. Каждый переключатель по умолчанию равен `false` и работает независимо. `keep_previous: 1` и `download_ahead: 3` задают окно серий, `retention_seconds: 1209600` — срок 14 дней. Закройте IINA и повторите установку для применения изменений; настройки и кэш сохранятся.
3. В конфигурации IINA задайте:

   ```ini
   server_cache=yes
   companion_url=http://SERVER_MAC.local:8092
   companion_token=YOUR_PRIVATE_TOKEN
   ```

   Скопируйте `token` из частной серверной конфигурации. Не публикуйте его. Перезапустите IINA.

4. Откройте серию из Lampa — её торрент попадёт в очередь. Готовые данные читаются с диска, недостающие поступают через TorrServer. При `progress=yes` неудавшиеся записи прогресса сохраняются и отправляются после восстановления TorrServer.

При включении: предел кэша — **64 ГиБ**, резерв места — **10 ГиБ**, срок — **14 дней** после последнего просмотра. При нехватке места скачивание приостанавливается. Файлы лежат в `~/Library/Caches/iina-torrserver/`, отдельно от кэша TorrServer. Помощник требует токен; используйте домашнюю сеть.

### Перемотка и особенности

**TorrServer загружает куски возле позиции просмотра, преимущественно впереди.** При перемотке в конец не нужно сначала скачивать всё начало.

**Наше скачивание на диск идёт последовательно:** с начала файла или с конца сохранённой части. Перемотка не переносит его; недостающие для просмотра данные поступают через TorrServer. Сначала скачивается текущая серия, затем максимум три следующие и сохранённая предыдущая. Если скачиваемый файл вышел за это окно, загрузка останавливается после текущего ограниченного запроса; полученные данные не дописываются.

Перемотка за 95% считается просмотром. Эта отметка больше не удаляет текущую или предыдущую серию: очистка следует порядку списка, включая пропущенные серии. Память озвучки общая для торрента. Внешние аудио и субтитры скачиваются, но автоматически не подключаются. Поддерживаются ссылки `/stream` и `/play`, но не списки `edl://`. При недоступности помощника до начала просмотра плеер возвращается к TorrServer; сбой во время просмотра может прервать воспроизведение.

Чтобы отключить IINA от кэша, задайте `server_cache=no`. Для остановки прежних фоновых заданий и очистки выключите все три серверных переключателя и повторите установку при закрытой IINA. Для удаления дополнения плеера удалите его Lua-файл и конфигурацию, затем перезапустите IINA.

### Проверки и лицензия

Автоматические тесты проверяют загрузку, воспроизведение, очистку, настройки и сохранность кэша. Отдельные проверки используют настоящий движок IINA и локальный тестовый торрент. Файлы — в [`tests/`](tests).

[MIT](LICENSE): можно использовать, изменять, распространять и продавать, в том числе в закрытых проектах. Сохраните текст лицензии. Гарантий нет.
