#!/bin/sh
# OH2P 1.62.2 的短期实验适配；由 Bridge 通过已有 run_shell 发送，不安装到固件。
# 轮询不能构成原厂资源锁。观察到原厂接管后，只结束自己的 curl，不关闭原厂灯效。
. /usr/share/libubox/jshn.sh || exit 10
mode=$1
url=$2
seconds=$3
case "$mode" in listening) light=41;; thinking) light=2;; check) light=0;; *) exit 11;; esac
case "$mode" in
    listening) expected_leds='stored led ids: 41(0) <- ; current id 41';;
    thinking) expected_leds='stored led ids: ; current id 2';;
esac
case "$seconds" in ''|*[!0-9]*) exit 12;; esac
[ "$seconds" -ge 1 ] && [ "$seconds" -le 60 ] || exit 12
[ "$mode" = check ] || case "$url" in http://*) ;; *) exit 13;; esac
child=
shown=0
taken=0

native_idle() {
    # 同一个进程、同一个 FIFO 的写端才视为占用；只读端不阻止实验。
    pns=$(pidof mipns-xiaomi) || return 1
    case "$pns" in ''|*[!0-9]*) return 1;; esac
    [ -d "/proc/$pns/fd" ] || return 1
    [ -p /tmp/mic_audio.fifo ] && [ ! -L /tmp/mic_audio.fifo ] || return 1
    for fd in /proc/"$pns"/fd/*; do
        target=$(readlink "$fd") || continue
        [ "$target" = /tmp/mic_audio.fifo ] || continue
        flags=
        while read key value rest; do
            [ "$key" = flags: ] && flags=$value
        done < /proc/"$pns"/fdinfo/"${fd##*/}"
        case "$flags" in ''|*[!0-7]*) return 1;; esac
        [ "$((flags & 3))" = 0 ] || return 1
    done
}

snapshot() {
    reply=$(ubus -t 1 call mediaplayer player_get_play_status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var code code
    [ "$code" = 0 ] || return 1
    json_get_var info info
    json_load "$info" || return 1
    json_get_var media status
    # 暂停为 2，原厂结束路径还会返回 3；原厂会话和灯效另行检查。
    case "$media" in 0|2|3) ;; *) return 1;; esac
    reply=$(ubus -t 1 call led status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var leds info
}

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ -n "$child" ]; then
        # 只处理本 shell 启动且尚未 wait 的直接子进程，禁止按名称扫描/杀进程。
        kill "$child" 2>/dev/null
        wait "$child" 2>/dev/null
        child=
    fi
    if [ "$shown" = 1 ] && [ "$taken" = 0 ] && native_idle && snapshot; then
        case "$leds" in
            "$expected_leds"|'stored led ids: ; current id 0')
                [ "$mode" != listening ] || ubus -t 1 call mic_audio set_event '{"event":1,"value":2}' >/dev/null
                ubus -t 1 call led shut "{\"L\":$light}" >/dev/null
                ;;
        esac
    fi
    printf 'visual_result=%s native_takeover=%s\n' "$result" "$taken"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

native_idle && snapshot || exit 20
[ "$leds" = 'stored led ids: ; current id 0' ] || exit 21
[ "$mode" != check ] || exit 0
shown=1
if [ "$mode" = listening ]; then
    ubus -t 1 call mic_audio set_event '{"event":1,"value":1}' >/dev/null || exit 22
fi
ubus -t 1 call led show "{\"L\":$light}" >/dev/null || exit 23

read started unused < /proc/uptime
deadline=$((${started%%.*} + seconds))
if [ "$mode" = listening ]; then
    # 打开 FIFO 也在被监护的子进程内；不能只给 curl 的网络操作设置超时。
    sh -c '[ -p /tmp/mic_audio.fifo ] && [ ! -L /tmp/mic_audio.fifo ] || exit 24; exec curl --silent --fail --no-buffer --noproxy "*" --connect-timeout 2 --max-time "$2" "$1" > /tmp/mic_audio.fifo' sh "$url" "$seconds" &
else
    sh -c 'exec curl --silent --fail --no-buffer --noproxy "*" --connect-timeout 2 --max-time "$2" "$1" > /dev/null' sh "$url" "$seconds" &
fi
child=$!
while kill -0 "$child" 2>/dev/null; do
    read now unused < /proc/uptime
    [ "${now%%.*}" -lt "$deadline" ] || exit 25
    if ! native_idle; then printf 'visual_abort=native_writer\n'; taken=1; exit 26; fi
    if ! snapshot; then printf 'visual_abort=media_or_status\n'; taken=1; exit 26; fi
    case "$leds" in
        "$expected_leds") ;;
        *) printf 'visual_abort=led_changed\n'; taken=1; exit 27;;
    esac
    sleep 0.1
done
wait "$child"
result=$?
child=
exit "$result"
