#!/usr/bin/env bash
# 安装 claude / claude-trace 包装到你的 shell。
#   ./install.sh           # 自动检测当前 shell
#   ./install.sh bash      # 指定 bash / zsh / fish
#
# 安装做的事:把一行 `source <repo>/shell/claude-trace.{sh,fish}` 追加到对应 rc 文件
# (bash→~/.bashrc, zsh→~/.zshrc, fish→~/.config/fish/config.fish)。幂等,不会重复写。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"

shell="${1:-}"
if [ -z "$shell" ]; then
  case "$(basename "${SHELL:-}")" in
    fish) shell=fish ;;
    zsh)  shell=zsh ;;
    *)    shell=bash ;;
  esac
fi

case "$shell" in
  bash) rc="$HOME/.bashrc";                  line="source $REPO/shell/claude-trace.sh" ;;
  zsh)  rc="$HOME/.zshrc";                   line="source $REPO/shell/claude-trace.sh" ;;
  fish) rc="$HOME/.config/fish/config.fish"; line="source $REPO/shell/claude-trace.fish" ;;
  *)    echo "未知 shell: $shell (支持 bash/zsh/fish)"; exit 1 ;;
esac

mkdir -p "$(dirname "$rc")"
touch "$rc"
if grep -qF "$line" "$rc"; then
  echo "✓ 已安装(rc 里已有该 source 行):$rc"
else
  printf '\n# Claude Code 抓包包装 (myproxy)\n%s\n' "$line" >> "$rc"
  echo "✓ 已写入 $rc"
fi
echo
echo "下一步:"
echo "  1) 新开终端,或 source $rc"
echo "  2) 启动抓包代理:  cd $REPO && ./start-mitm.sh"
echo "  3) 任意目录直接 claude(代理在跑就自动被 dump,没跑则照常启动)"
