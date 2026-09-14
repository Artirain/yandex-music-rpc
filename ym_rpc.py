import base64
import json
import os
import struct
import subprocess
import time
import urllib.parse
import urllib.request
import uuid


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), value.strip())


load_env()
CLIENT_ID = os.environ["DISCORD_CLIENT_ID"]
SOURCE_APP = "ru.yandex.desktop.music"

SMTC_SCRIPT = r"""
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
while ($true) {
  $s = $mgr.GetSessions() | Where-Object { $_.SourceAppUserModelId -eq '__SOURCE_APP__' } | Select-Object -First 1
  if ($s) {
    $p = Await ($s.TryGetMediaPropertiesAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
    $tl = $s.GetTimelineProperties()
    $playing = $s.GetPlaybackInfo().PlaybackStatus.ToString() -eq 'Playing'
    $pos = $tl.Position.TotalSeconds
    if ($playing) { $pos += ([DateTimeOffset]::UtcNow - $tl.LastUpdatedTime).TotalSeconds }
    [Console]::WriteLine((@{ artist = $p.Artist; title = $p.Title; playing = $playing; pos = $pos; dur = $tl.EndTime.TotalSeconds } | ConvertTo-Json -Compress))
  } else {
    [Console]::WriteLine('{}')
  }
  Start-Sleep -Seconds 2
}
"""


def media_states():
    script = SMTC_SCRIPT.replace("__SOURCE_APP__", SOURCE_APP)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for line in proc.stdout:
        line = line.strip().lstrip("\ufeff")
        if line:
            yield json.loads(line) or None


class Discord:
    def __init__(self):
        self.pipe = None

    def connect(self):
        for i in range(10):
            try:
                self.pipe = open(rf"\\.\pipe\discord-ipc-{i}", "r+b", buffering=0)
                break
            except OSError:
                continue
        else:
            raise OSError("discord is not running")
        self._send(0, {"v": 1, "client_id": CLIENT_ID})

    def _read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.pipe.read(size - len(data))
            if not chunk:
                raise OSError("discord pipe closed")
            data += chunk
        return data

    def _send(self, op, payload):
        data = json.dumps(payload).encode()
        self.pipe.write(struct.pack("<II", op, len(data)) + data)
        _, length = struct.unpack("<II", self._read(8))
        return json.loads(self._read(length))

    def set_activity(self, activity):
        if self.pipe is None:
            self.connect()
        try:
            reply = self._send(1, {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": str(uuid.uuid4()),
            })
        except OSError:
            self.pipe = None
            raise
        if reply.get("evt") == "ERROR":
            print("discord error:", reply.get("data"))


def find_track(artist, title):
    query = urllib.parse.urlencode({"text": f"{artist} {title}", "type": "track", "page": 0})
    try:
        with urllib.request.urlopen(f"https://api.music.yandex.net/search?{query}", timeout=5) as resp:
            results = json.load(resp)["result"].get("tracks", {}).get("results", [])
    except (OSError, ValueError, KeyError):
        return None
    if not results:
        return None
    track = results[0]
    album_id = track["albums"][0]["id"] if track.get("albums") else None
    return {
        "cover": "https://" + track["coverUri"].replace("%%", "400x400") if track.get("coverUri") else None,
        "url": f"https://music.yandex.ru/album/{album_id}/track/{track['id']}" if album_id else None,
    }


def build_activity(state, track):
    title, artist = state["title"], state["artist"]
    activity = {
        "type": 2,
        "details": (title if state["playing"] else f"(Пауза) {title}")[:128],
        "state": artist[:128],
    }
    if state["playing"] and state["dur"] > 0:
        start = time.time() - state["pos"]
        activity["timestamps"] = {"start": int(start * 1000), "end": int((start + state["dur"]) * 1000)}
    if track and track["cover"]:
        activity["assets"] = {"large_image": track["cover"], "large_text": title[:128]}
    if track and track["url"]:
        activity["buttons"] = [{"label": "Слушать в Яндекс Музыке", "url": track["url"]}]
    return activity


def main():
    discord = Discord()
    tracks = {}
    last_sig, last_start = None, 0.0
    while True:
        for state in media_states():
            if state and not state["title"]:
                state = None
            sig = (state["artist"], state["title"], state["playing"]) if state else None
            start = time.time() - state["pos"] if state else 0.0
            seeked = state and state["playing"] and abs(start - last_start) > 3
            if sig == last_sig and not seeked:
                continue
            activity = None
            if state:
                key = (state["artist"], state["title"])
                if key not in tracks:
                    tracks[key] = find_track(*key)
                activity = build_activity(state, tracks[key])
            try:
                discord.set_activity(activity)
            except OSError as e:
                print("discord unavailable:", e)
                continue
            last_sig, last_start = sig, start
            print(f"{'▶' if state and state['playing'] else '⏸'} {state['artist']} — {state['title']}" if state else "■ nothing playing")
        time.sleep(2)


if __name__ == "__main__":
    main()
