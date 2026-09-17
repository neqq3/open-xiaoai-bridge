#!/bin/sh
# LX06 1.94.13 静态分析原型。仅显式实验 profile 使用；不修改固件、不回送麦克风。
. /usr/share/libubox/jshn.sh || exit 10
mode=$1
url=$2
seconds=$3
case "$mode" in listening) light=1;; thinking) light=2;; check) light=0;; *) exit 11;; esac
case "$seconds" in ''|*[!0-9]*) exit 12;; esac
[ "$seconds" -ge 1 ] && [ "$seconds" -le 60 ] || exit 12
[ "$mode" = check ] || case "$url" in http://*) ;; *) exit 13;; esac
child=
control=
shown=0
taken=0
clear_allowed=0

snapshot() {
    # LX06 的 status 没有 code 字段；info 为空或嵌套 JSON effect 数组。
    reply=$(ubus -t 1 call led status '{}') || return 1
    json_load "$reply" || return 1
    json_get_type kind info
    [ "$kind" = string ] || return 1
    json_get_var info info
    state=idle
    [ -n "$info" ] || return 0
    json_load "$info" || return 1
    json_get_type kind effect
    [ "$kind" = array ] || return 1
    json_select effect || return 1
    json_get_keys keys
    [ -n "$keys" ] || return 0
    # 只接受单个与本轮完全匹配的动画，出现额外动画便让出控制。
    [ "$keys" = 1 ] || { state=foreign; return 0; }
    json_select 1 || return 1
    json_get_type kind type
    [ "$kind" = int ] || return 1
    json_get_var current type
    json_get_type kind pos
    [ "$kind" = int ] || return 1
    json_get_var pos pos
    if [ "$current" = "$light" ] && [ "$pos" = 0 ]; then state=ours; else state=foreign; fi
}

player_idle() {
    reply=$(ubus -t 1 call mediaplayer player_get_play_status '{}') || return 1
    json_load "$reply" || return 1
    json_get_var code code
    [ "$code" = 0 ] || return 1
    json_get_var info info
    json_load "$info" || return 1
    json_get_var status status
    [ "$status" = 0 ]
}

cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ -n "$child" ]; then kill "$child" 2>/dev/null; wait "$child"; fi
    # 只有 Bridge 明确发回 C，且当前状态仍匹配，才撤销自己申请的动画。
    # 原厂回调发 P；断网/进程终止没有结束标记时，不盲目关闭未知归属灯效。
    if [ "$shown" = 1 ] && [ "$taken" = 0 ] && [ "$clear_allowed" = 1 ]; then
        if player_idle && snapshot && [ "$state" = ours ]; then
            ubus -t 1 call led shut "{\"L\":$light}" >/dev/null || result=28
        else
            taken=1
            result=27
        fi
    fi
    [ -z "$control" ] || rm -f "$control"
    printf 'visual_result=%s native_takeover=%s\n' "$result" "$taken"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

player_idle && snapshot || exit 20
[ "$state" = idle ] || exit 21
[ "$mode" != check ] || exit 0
control=$(mktemp /tmp/bridge-lx06-visual.XXXXXX) || exit 22
shown=1
ubus -t 1 call led show "{\"L\":$light,\"pos\":0}" >/dev/null || exit 23
curl --silent --fail --no-buffer --noproxy '*' --connect-timeout 2 --max-time "$((seconds + 2))" "$url" > "$control" &
child=$!
read started unused < /proc/uptime
deadline=$((${started%%.*} + seconds + 2))
while kill -0 "$child" 2>/dev/null; do
    read now unused < /proc/uptime
    [ "${now%%.*}" -lt "$deadline" ] || exit 24
    if ! player_idle || ! snapshot || [ "$state" != ours ]; then taken=1; exit 27; fi
    sleep 0.1
done
wait "$child"
result=$?
child=
[ "$result" = 0 ] || exit 24
marker=$(tail -c 1 "$control")
case "$marker" in
    C) clear_allowed=1; exit 0;;
    P) taken=1; exit 26;;
    *) exit 24;;
esac
