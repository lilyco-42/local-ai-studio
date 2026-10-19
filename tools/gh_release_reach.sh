#!/usr/bin/env bash
# 从"用户那一侧的出口"量 GitHub release 产物：可达？支持 Range？真吞吐多少？
# URL 不手抄 —— 由 toolbelt_pins.py 从 pins/toolbelt.json 直接给出。
#
#   ssh -i ~/.ssh/lain42.pem root@lain42.top 'bash -s' \
#     -- $(python tools/toolbelt_pins.py --print-urls | tr '\n' ' ') < tools/gh_release_reach.sh
#
# 本机跑出来的数不代表国内出口，所以这条必须在服务器上跑。
# 结构：每条产物先取 64 KiB（判可达 + Accept-Ranges + 声明总长），再取 1 MiB（判吞吐），
# 最后一条负向对照必须 404 —— 对照不成立时前面所有的"可达"都不算数。
set -u
echo "host=$(hostname) date=$(date -u +%FT%TZ) 采样数=$#"

for spec in "$@"; do
  key=${spec%%|*}; rest=${spec#*|}; url=${rest%%|*}; size=${rest#*|}
  read -r code_s got_s t_s < <(curl -sSL --max-time 180 -r 0-65535 -o /dev/null \
      -D /tmp/h.$$ -w '%{http_code} %{size_download} %{time_total}\n' "$url")
  ar=$(tr -d '\r' < /tmp/h.$$ | grep -ic '^accept-ranges: bytes' || true)
  cr=$(tr -d '\r' < /tmp/h.$$ | grep -i '^content-range:' | head -1 || true)
  rm -f /tmp/h.$$
  read -r got_b t_b spd_b ttfb < <(curl -sSL --max-time 300 -r 0-1048575 -o /dev/null \
      -w '%{size_download} %{time_total} %{speed_download} %{time_starttransfer}\n' "$url")
  awk -v k="$key" -v c="$code_s" -v g="$got_s" -v ts="$t_s" -v ar="$ar" -v cr="$cr" \
      -v size="$size" -v gb="$got_b" -v tb="$t_b" -v sp="$spd_b" -v tf="$ttfb" 'BEGIN{
    printf "%s\n  64KiB code=%s got=%s t=%ss Accept-Ranges=%s [%s]\n", k, c, g, ts, (ar>0?"bytes":"没有"), cr
    if (sp+0 > 0)
      printf "  1MiB  got=%s t=%ss ttfb=%ss → %.3f MiB/s；整包 %s B 外推约 %.0f s（%.1f min）\n",
             gb, tb, tf, sp/1048576, size, size/sp, size/sp/60
    else printf "  1MiB  没取到（spd=%s）\n", sp
  }'
done

echo "负向对照（必须 404）"
code_n=$(curl -sSL --max-time 60 -o /dev/null -w '%{http_code}' \
  "https://github.com/AlexsJones/llmfit/releases/download/v1.1.17/llmfit-v1.1.17-nope-none.zip" || echo ERR)
echo "  不存在的产物 code=$code_n"
test "$code_n" = "404" || { echo "  ✗ 对照不成立，上面的数不能当证据"; exit 1; }
