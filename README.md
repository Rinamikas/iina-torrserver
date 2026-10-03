# IINA TorrServer Companion

Watch a torrent season in IINA, keep your preferred audio track, and send playback progress back to TorrServer for Lampa.

- **Episodes in IINA:** open one episode; the torrent's video files appear in the native playlist, naturally sorted (2 before 10). Your current episode stays selected; existing playlists are preserved.
- **Choose a voice once:** a manual audio selection is remembered per torrent, including after restart. Tracks are matched by title and language, with codec/channel tie-breaking, rather than unstable track numbers. Missing or ambiguous matches keep IINA's default.
- **Progress back to Lampa:** saved every 15 seconds, on pause, stop and exit; reaching the end saves the full duration.
- **One Lua file:** no Lampa changes, playlist export, cloud account or background service. Only explicitly configured server addresses receive requests.

## How it differs

[mpv-torrserver-loader](https://github.com/pursvir/mpv-torrserver-loader) focuses on browsing torrents and loading external audio/subtitles. [lampa-mx-iina-playlist](https://github.com/Rogengo/lampa-mx-iina-playlist) provides a Lampa-side playlist export. This companion combines IINA's native episode navigation, per-torrent audio memory and TorrServer progress updates directly in the player.

## Install

Tested on macOS with **IINA 1.3.5** and **TorrServer MatriX.145.1**. Uses IINA's bundled mpv Lua support and macOS curl. The tested TorrServer connection does not require authentication.

1. Download this repository (Code → Download ZIP), extract it, and open Terminal in the extracted folder. Copy the files:

   ```sh
   mkdir -p "$HOME/.config/mpv/scripts" "$HOME/.config/mpv/script-opts"
   cp torrserver-progress.lua "$HOME/.config/mpv/scripts/"
   cp script-opts/torrserver-progress.conf "$HOME/.config/mpv/script-opts/"
   ```

   If IINA already uses a custom configuration folder, use that folder instead of `~/.config/mpv`.

2. Edit `script-opts/torrserver-progress.conf` inside your configuration folder. Set `servers` to the exact server URL used in your playback links, including port. Comma-separated aliases are supported:

   ```ini
   servers=http://torrserver.local:8090,http://127.0.0.1:8090
   ```

3. In **IINA → Settings → Advanced**, enable advanced settings and **Use config directory**, select that configuration folder, then fully quit and reopen IINA.
4. Enable **TrackTimecode** in both TorrServer and Lampa. In Lampa, select IINA as the external player and open an episode. Open IINA's playlist to switch episodes; automatic next-episode playback follows IINA's own settings.

## Limits and removal

Lampa reads the saved progress when you **reopen the torrent's file list**; an already-open list does not refresh live. Audio memory covers a whole torrent, so seasons sharing one torrent share the preference. Untitled tracks with identical languages cannot reliably distinguish voices. Direct `/stream?...link=…&index=…` and `/play/<hash>/<index>` links are supported; `edl://` playlists are not.

No download scheduling, cache cleanup or offline progress queue. Audio preferences stay in `torrserver-audio.json` inside your configuration folder. Remove `scripts/torrserver-progress.lua` and its configuration file, then restart IINA to uninstall.

## License

[MIT](LICENSE): use, modify, redistribute and sell, including in closed-source projects. Retain the copyright and license notice; provided without warranty.
