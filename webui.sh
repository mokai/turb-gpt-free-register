#!/usr/bin/env bash
set -euo pipefail

# Turb GPT Free Register WebUI 管理脚本
#
# 用法：
#   ./webui.sh start      启动 WebUI
#   ./webui.sh stop       关闭 WebUI
#   ./webui.sh restart    重启 WebUI
#   ./webui.sh status     查看状态
#   ./webui.sh logs       实时查看日志
#
# 可选环境变量：
#   HOST=127.0.0.1
#   PORT=5000
#   OPEN_BROWSER=1
#   VERBOSE=1
#   AUTH_CODE=xxx
#   EXTRA_ARGS="..."

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ── 启动前环境清理（务必保留）────────────────────────────────────────────
# 1) Agent 沙箱（WorkBuddy / CodeBuddy 等）注入的变量。NODE_OPTIONS 指向
#    node-language-shim.cjs，配合 CODEBUDDY_BROKERED_FS_HOOK_ENABLED=1 与
#    CODEBUDDY_SANDBOX_BROKER_IPC_ADDRESS，会让 WebUI 的 node 子进程
#    （sentinel 生成 token）被 brokered-fs shim 接管；而那个 broker socket 只属于
#    启动 WebUI 的那个 Agent 会话，会话结束即 "connect failed: Connection refused"
#    → 查活/注册会在 sentinel 步骤 100% 失败。从 Agent 会话里重启 WebUI 时尤其要清。
# 2) shell 层代理。WebUI 的出口代理按账号显式配置，继承 HTTP_PROXY 会把 Selenium /
#    curl_cffi 一起拖下水（no_proxy 写 '*' 更会连显式代理都绕过）。
for _v in NODE_OPTIONS NODE_REPL_EXTERNAL_MODULE \
          HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
          NO_PROXY no_proxy; do
  unset "$_v" 2>/dev/null || true
done
while IFS='=' read -r _k _rest; do
  case "$_k" in
    CODEBUDDY_*|SANDBOX_*|TOYBOX_*|CLAUDE_*|GENIE_*) unset "$_k" 2>/dev/null || true ;;
  esac
done < <(env)
export no_proxy="127.0.0.1,localhost,::1"
unset _v _k _rest

RUN_DIR="$ROOT_DIR/run"
LOG_DIR="$ROOT_DIR/logs"
PID_FILE="$RUN_DIR/webui.pid"
LOG_FILE="$LOG_DIR/webui.log"

HOST="${HOST:-127.0.0.1}"
# 默认 5001：本机 macOS「隔空播放接收器」(ControlCenter/AirTunes) 监听 *:5000 的 IPv6，
# 浏览器把 localhost 解析成 ::1 时会被它抢答 403。除非已关闭隔空播放接收器，否则勿改回 5000。
PORT="${PORT:-5001}"
OPEN_BROWSER="${OPEN_BROWSER:-0}"
VERBOSE="${VERBOSE:-0}"
AUTH_CODE="${AUTH_CODE:-}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

usage() {
  cat <<EOF
用法：$0 <command>

commands:
  start      启动 WebUI
  stop       关闭 WebUI
  restart    重启 WebUI
  status     查看运行状态
  logs       实时查看日志

环境变量：
  HOST=127.0.0.1 PORT=5000 OPEN_BROWSER=1 VERBOSE=1 AUTH_CODE=xxx EXTRA_ARGS="..."

示例：
  ./webui.sh start
  PORT=8000 OPEN_BROWSER=1 ./webui.sh start
  HOST=0.0.0.0 PORT=5000 ./webui.sh restart
EOF
}

is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

read_pid() {
  [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null || true
}

find_pids_by_port() {
  pgrep -f "python.*web\.py.*--port[ =]${PORT}" 2>/dev/null || true
}

get_python() {
  local venv_python="$ROOT_DIR/.venv/bin/python"
  if [[ -x "$venv_python" ]] && "$venv_python" -c 'import sys; print(sys.executable)' >/dev/null 2>&1; then
    echo "$venv_python"
  elif [[ -e "$venv_python" ]]; then
    echo "检测到 .venv 存在，但虚拟环境中的 Python 已失效。" >&2
    echo "请重新创建虚拟环境并安装依赖：" >&2
    echo "  rm -rf .venv && python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
    return 1
  elif command -v python3 >/dev/null 2>&1; then
    local system_python
    system_python="$(command -v python3)"
    if "$system_python" -c 'import flask' >/dev/null 2>&1; then
      echo "$system_python"
    else
      echo "未找到可用 Python 环境：系统 Python 缺少 Flask，请先创建 .venv 并安装 requirements.txt" >&2
      return 1
    fi
  else
    echo "未找到 Python：请先创建 .venv 或安装 python3" >&2
    return 1
  fi
}

collect_running_pids() {
  local pids=()
  local pid
  pid="$(read_pid)"
  if is_running "$pid"; then
    pids+=("$pid")
  fi

  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(find_pids_by_port)

  local unique=()
  local seen x
  for pid in "${pids[@]:-}"; do
    [[ -z "$pid" || "$pid" == "$$" ]] && continue
    seen=0
    for x in "${unique[@]:-}"; do
      [[ "$x" == "$pid" ]] && seen=1 && break
    done
    [[ "$seen" == "0" ]] && unique+=("$pid")
  done

  printf '%s\n' "${unique[@]:-}"
}

cmd_start() {
  local old_pid py pid
  old_pid="$(read_pid)"
  if is_running "$old_pid"; then
    echo "WebUI 已在运行：PID=${old_pid}，地址：http://${HOST}:${PORT}"
    return 0
  fi
  rm -f "$PID_FILE"

  py="$(get_python)"

  local args=("web.py" "--host" "$HOST" "--port" "$PORT")
  if [[ "$OPEN_BROWSER" == "1" || "$OPEN_BROWSER" == "true" ]]; then
    args+=("--open-browser")
  fi
  if [[ "$VERBOSE" == "1" || "$VERBOSE" == "true" ]]; then
    args+=("--verbose")
  fi
  if [[ -n "$AUTH_CODE" ]]; then
    args+=("--auth-code" "$AUTH_CODE")
  fi
  if [[ -n "$EXTRA_ARGS" ]]; then
    # shellcheck disable=SC2206
    local extra_parts=($EXTRA_ARGS)
    args+=("${extra_parts[@]}")
  fi

  echo "启动 WebUI：http://${HOST}:${PORT}"
  echo "日志文件：$LOG_FILE"
  # 必须 start_new_session=True 起子进程：只靠 nohup + & 时，WebUI 仍留在调用方
  # shell 的会话/进程组里。从 Agent 会话（或其 Bash 工具）里重启时，命令一返回
  # 整个进程组就被回收 —— 表现为"启动成功：PID=xxxx"，随后进程凭空消失、
  # 5001 不再监听、日志停在最后一条请求上（没有任何 traceback）。
  pid="$("$py" - "$py" "$LOG_FILE" "${args[@]}" <<'PY'
import subprocess, sys

python, log_file, *args = sys.argv[1:]
with open(log_file, "ab") as fh:
    proc = subprocess.Popen(
        [python, *args],
        stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
print(proc.pid)
PY
)"
  echo "$pid" > "$PID_FILE"

  sleep 1
  if is_running "$pid"; then
    echo "启动成功：PID=$pid"
  else
    echo "启动失败，请查看日志：$LOG_FILE" >&2
    rm -f "$PID_FILE"
    return 1
  fi
}

cmd_stop() {
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "正在关闭 WebUI：PID=${pids[*]}"
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done

  local alive
  for _ in {1..15}; do
    alive=0
    for pid in "${pids[@]}"; do
      if is_running "$pid"; then
        alive=1
        break
      fi
    done
    [[ "$alive" == "0" ]] && break
    sleep 1
  done

  for pid in "${pids[@]}"; do
    if is_running "$pid"; then
      echo "进程未退出，强制结束：PID=$pid"
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done

  rm -f "$PID_FILE"
  echo "已关闭 WebUI"
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

cmd_status() {
  local pids=()
  local pid
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && pids+=("$pid")
  done < <(collect_running_pids)

  if [[ "${#pids[@]}" -eq 0 ]]; then
    echo "WebUI 未运行"
    return 1
  fi

  echo "WebUI 运行中：PID=${pids[*]}"
  echo "地址：http://${HOST}:${PORT}"
  echo "日志：$LOG_FILE"
}

cmd_logs() {
  touch "$LOG_FILE"
  tail -n 120 -f "$LOG_FILE"
}

cmd="${1:-}"
case "$cmd" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs|log) cmd_logs ;;
  -h|--help|help|"") usage ;;
  *)
    echo "未知命令：$cmd" >&2
    usage >&2
    exit 2
    ;;
esac
