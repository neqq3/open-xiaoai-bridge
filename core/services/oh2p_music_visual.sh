#!/bin/sh
# 临时任务：只使用原厂工具，不安装程序、不修改启动或原厂配置。
set -f
mode=$1 base=$2 token=$3 seconds=$4
case "$mode" in check|live) ;; *) exit 2;; esac
# 所有提前拒绝也返回结构化完成标记，便于区分设备拒绝和 RPC 丢失。
trap 'printf "music_result=%s frames=0 reason=preflight\n" "$?"' EXIT
if [ "$mode" = live ]; then
    case "$base" in http://*) ;; *) exit 2;; esac
    case "$token" in ''|*[!a-f0-9]*) exit 2;; esac
    [ "${#token}" = 32 ] || exit 2
    case "$seconds" in ''|*[!0-9]*) exit 2;; esac
    [ "$seconds" -ge 1 ] && [ "$seconds" -le 120 ] || exit 2
fi
[ "$(micocfg_model)" = OH2P ] || exit 3
grep -q 'Ver:1\.62\.2[[:space:]]*$' /etc/banner || exit 3
. /usr/share/libubox/jshn.sh || exit 3
safe() {
    reply=$(ubus -t 1 call led status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var state info
    [ "$state" = 'stored led ids: ; current id 0' ] || return 1
    reply=$(ubus -t 1 call mediaplayer get_playled_status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var code code
    json_get_var setting info
    [ "$code" = 0 ] && [ "$setting" = 0 ]
}
playing() {
    reply=$(ubus -t 1 call mediaplayer player_get_play_status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var code code
    json_get_var info info
    [ "$code" = 0 ] || return 1
    json_load "$info" || return 1
    json_get_var state status
    json_get_var media media_type
    [ "$state" = 1 ] && [ "$media" = 3 ]
}
command -v arecord >/dev/null && command -v curl >/dev/null || exit 3
[ -w /sys/devices/i2c-2/2-0034/led_rgb ] || exit 3
safe || { echo 'native_busy'; exit 4; }
[ ! -d /tmp/xiaomi-spectrum-oh2p-service ] || exit 4
playing || exit 4
[ "$mode" != check ] || exit 0
# 原子目录锁防止多个 Bridge 实例同时启动；正常退出时只清理自身锁。
lock=/tmp/bridge-music-visual.lock
mkdir "$lock" 2>/dev/null || exit 4
dir=$(mktemp -d /tmp/bridge-spectrum.XXXXXX) || { rmdir "$lock"; exit 5; }
rec= upload= download= guard=
shown=0 count=0 previous=0 reason=stream_lost
node=/sys/devices/i2c-2/2-0034/led_rgb
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    for pid in "$rec" "$upload" "$download" "$guard"; do
        [ -z "$pid" ] || kill "$pid" 2>/dev/null
    done
    for pid in "$rec" "$upload" "$download" "$guard"; do
        [ -z "$pid" ] || wait "$pid" 2>/dev/null
    done
    if [ "$shown" = 1 ] && [ ! -f "$dir/takeover" ] && safe; then
        i=0
        while [ "$i" -lt 12 ]; do
            printf '%s 0x000000\n' "$i" > "$node"
            i=$((i+1))
        done
    fi
    [ ! -f "$dir/takeover" ] || reason=native_takeover
    [ ! -f "$dir/deadline" ] || reason=deadline
    rm -f "$dir/audio" "$dir/frames" "$dir/takeover" "$dir/deadline"
    rmdir "$dir"
    rmdir "$lock"
    printf 'music_result=%s frames=%s reason=%s mode=%s\n' "$rc" "$count" "$reason" "$mode"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 9' HUP INT TERM
mkfifo "$dir/audio" "$dir/frames" || exit 5
parent=$$
read uptime unused < /proc/uptime
deadline=$((${uptime%%.*}+seconds+3))
(
    while :; do
        read uptime unused < /proc/uptime
        if [ "${uptime%%.*}" -ge "$deadline" ]; then
            touch "$dir/deadline"; kill -TERM "$parent"; exit
        fi
        if ! safe; then
            touch "$dir/takeover"; kill -TERM "$parent"; exit
        fi
        if ! playing; then
            kill -TERM "$parent"; exit
        fi
        sleep .1
    done
) &
guard=$!
arecord -q -t raw -D hw:0,2 -f S16_LE -r 48000 -c 2 --buffer-size=4096 --period-size=1024 -d "$seconds" > "$dir/audio" &
rec=$!
curl -sS --fail --noproxy '*' --connect-timeout 2 --max-time "$seconds" -H 'Expect:' -T - "$base/music-visual/audio/$token" < "$dir/audio" > /dev/null &
upload=$!
curl -sS --fail --no-buffer --noproxy '*' --connect-timeout 2 --max-time "$seconds" "$base/music-visual/frames/$token" > "$dir/frames" &
download=$!
exec 3< "$dir/frames"
while IFS= read -t 3 -r line <&3; do
    case "$line" in
        STOP) reason=stopped; exit 0;;
        YIELD) touch "$dir/takeover"; exit 0;;
    esac
    [ "${#line}" -le 120 ] || { reason=bad_frame; exit 6; }
    set -- $line
    [ "$#" = 13 ] || { reason=bad_frame; exit 6; }
    seq=$1; shift
    case "$seq" in ''|*[!0-9]*) exit 6;; esac
    [ "${#seq}" -le 8 ] && [ "$seq" -gt "$previous" ] || exit 6
    for rgb in "$@"; do
        [ "${#rgb}" = 8 ] || exit 6
        case "$rgb" in 0x??????) ;; *) exit 6;; esac
        case "${rgb#0x}" in *[!0-9A-F]*) exit 6;; esac
    done
    [ ! -f "$dir/takeover" ] || exit 0
    if [ "$mode" = live ]; then
        i=0
        for rgb in "$@"; do
            printf '%s %s\n' "$i" "$rgb" > "$node" || exit 7
            i=$((i+1))
        done
        shown=1
    fi
    previous=$seq
    count=$((count+1))
done
reason=stream_lost
exit 8
