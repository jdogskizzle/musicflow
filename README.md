# musicflow

Fills the gaps in a [Lidarr](https://lidarr.audio) music library from Soulseek (through
[slskd](https://github.com/slskd/slskd)), one artist at a time, and finishes each album the
way a careful manual workflow would:

| Step | What musicflow does |
|---|---|
| Find | Searches slskd for every missing (monitored, released) album and ranks results by title match, track count against the CD release, track lengths, your wanted format and how available the source is. You confirm each pick. |
| Import | Copies the files to `<downloads>/musicflow-staging/`, then imports them with Lidarr's manual import onto the **normal CD release with the fewest discs**, even when the download is a vinyl rip, and renames them with your Lidarr naming format. The downloaded originals are deleted only after Lidarr accepts them. |
| Lyrics | [LRCLIB](https://lrclib.net) lookup per track: synced lyrics to `.lrc`, plain to `.txt`, instrumentals to `[au: instrumental]`. Failed lookups are retried pass after pass until a pass recovers none. |
| Cover | The largest Apple Music artwork (usually 3000×3000+), saved as `cover.jpg`. Falls back to Cover Art Archive and says so. |
| Tags | Picard-style tags from MusicBrainz for the chosen release (IDs, credits, genres, label, barcode…) plus a 500px embedded cover. `date` is the Wikipedia infobox date when there is one. |
| Jellyfin | Scans the music library once a batch is done (optional). |
| Permissions | Album files get group read/write. |

## Setup

You need Docker with Compose, and Lidarr and slskd running in containers on one Docker network
(Jellyfin is optional).

```
git clone https://github.com/jdogskizzle/musicflow.git && cd musicflow
cp .env.example .env     # then edit it
docker compose up -d --build musicflow-web
```

In `.env`:

* **`MUSIC_DIR`, `DOWNLOADS_DIR`**: your library (Lidarr's root folder) and slskd's download folder on the host.
* **`MUSIC_PATH`, `DOWNLOADS_PATH`**: where *Lidarr's* container sees those folders (default `/music` and `/downloads`). musicflow mounts them at the same paths so Lidarr can import what it hands over.
* **`DOCKER_NETWORK`** and the **`*_URL`**s: the network your apps share and how to reach them on it. Add the URL base if you set one, e.g. `http://lidarr:8686/lidarr`.
* **API keys**: point `LIDARR_CONFIG` / `SLSKD_CONFIG` at the apps' config files and musicflow reads the keys from them, or paste `LIDARR_API_KEY` / `SLSKD_API_KEY`. `JELLYFIN_API_KEY` turns on library scans.
* **`PUID`, `PGID`**: the user and group new files are written as; match your media apps.

`.env` and `state/` hold your keys and history; both are git-ignored.

## Web app

Listens on port 8000 inside the container, with no login, so keep it on your home network.
Put it behind your reverse proxy at `/musicflow/`, e.g. with Caddy:

```
handle_path /musicflow/* {
	reverse_proxy musicflow-web:8000
}
```

* **Artists**: who has missing albums.
* **Artist page**: *Search N missing albums*, check the preselected best result for each (click a folder name to see its files, or search again with other words), then *Download and finish*. Owned albums have *Re-tag* / *Re-tag + new cover*.
* **Activity**: progress, the per-album report (date and where it came from, cover, lyrics), logs, Retry/Cancel/Dismiss. Downloads keep going with the page closed.
* **Import**: folders already in the slskd download folder (things you grabbed by hand).
* **Wanted format** (top right): *CD 16/44.1*, *Hi-res 24-bit* or *Any lossless*. Results in that format rank first and are preselected; others are shown in orange. When a source doesn't report bit depth, a bitrate over ~1450 kbps counts as hi-res.

Restart after code changes: `docker compose up -d --build musicflow-web`

## Terminal

```
./musicflow get "Artist"           # search, pick (Enter = best), download, finish
./musicflow get "Artist" --dry-run # just show what it would pick
./musicflow import "Artist" "/path/to/downloads/some folder"
./musicflow finish "Artist" [--album "Title"] [--force-cover]
./musicflow resume
./musicflow format [cd|hires|any]  # show or set the wanted format (get --format overrides it once)
```

When the web app is running, it does the downloading; `get` from the terminal adds its picks to the same queue.

## Notes

* Albums that are only partly in the library are skipped. Finish those in Lidarr.
* If Wikipedia's year differs from the year in Lidarr's folder name, the report says so. Fix the date on musicbrainz.org and refresh the artist in Lidarr to rename the folder.
* State lives in `state/`: `jobs.json` (queue), `settings.json`, `albums.json`, `searches.json`.

## License

MIT, see [LICENSE](LICENSE).
