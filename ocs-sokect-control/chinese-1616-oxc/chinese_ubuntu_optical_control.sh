#!/bin/bash

# 16X16 矩阵光开关控制脚本 (OXC-16X16-1U-LP)
# 严格按照《矩阵式16X16光开关 使用说明》中的 OSW 协议实现
#
# 协议要点 (见说明书 4.4 上位机监控说明):
#   1. 设备每次只能执行一个指令, 必须等待返回响应后才能发送下一条指令
#   2. 指令使用大写字母
#   3. 尖括弧 "<" 作为开始符, ">" 作为结束符
#   4. 不能同时有两路输入选择同一输出, 否则为非法命令 (返回 <ER>)
#
# 出厂缺省配置: IP 192.168.1.178 (TCP Server), 网关 192.168.1.1,
#               子网掩码 255.255.255.0, TCP端口 4001

set -uo pipefail  # 严格模式(移除-e以防止意外退出)

# ================== 设备配置区 (修改这里) ==================
SWITCH_IP="192.168.136.200"   # 光开关IP地址
SWITCH_PORT=4001              # 光开关TCP端口
# ==============================================================

# ===== 全局变量 =====
SOCKET_FD=""
# 当前光路状态表: SWITCH_STATE[i] = 输入端口i对应的输出端口 (00=不通光)
SWITCH_STATE=()
for ((i=1; i<=16; i++)); do
    SWITCH_STATE[i]="00"
done

# 批量操作解析结果
BATCH_INPUTS=()
BATCH_OUTPUTS=()

# ===== 颜色定义 =====
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ===== 日志函数 =====
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ===== 清理函数 =====
cleanup() {
    if [[ -n "$SOCKET_FD" ]]; then
        { exec {SOCKET_FD}>&-; } 2>/dev/null || true
        { exec {SOCKET_FD}<&-; } 2>/dev/null || true
        SOCKET_FD=""
    fi
}

trap cleanup EXIT INT TERM

# ===== 输入验证 =====
# 验证光端口号 (1-16)
validate_optical_port() {
    local port=$1
    if [[ "$port" =~ ^[0-9]+$ ]] && (( 10#$port >= 1 && 10#$port <= 16 )); then
        return 0
    fi
    return 1
}

# 确认对话框, 返回0表示确认
confirm() {
    local prompt=$1
    local answer
    echo -n "$prompt "
    read -r answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

# ===== 连接管理 =====
connect_switch() {
    log_info "正在连接到光开关 $SWITCH_IP:$SWITCH_PORT..."

    # 检测连接可达性
    if ! timeout 3 nc -z "$SWITCH_IP" "$SWITCH_PORT" 2>/dev/null; then
        log_error "无法连接到 $SWITCH_IP:$SWITCH_PORT (请检查设备电源/网络/防火墙)"
        return 1
    fi

    # 创建持久TCP连接 (重定向只作用于大括号组, 不能直接挂在exec上, 否则会永久改变shell的stderr)
    if { exec {SOCKET_FD}<>/dev/tcp/$SWITCH_IP/$SWITCH_PORT; } 2>/dev/null; then
        log_success "成功连接到光开关: $SWITCH_IP:$SWITCH_PORT"
        # 软校验: 设备应能响应 <INFO_?>
        local check
        check=$(send_command "<INFO_?>" 3)
        if [[ "$check" == "TIMEOUT" || -z "$check" ]]; then
            log_warn "TCP已连接, 但设备未响应 <INFO_?> (请确认设备型号与协议)"
        fi
        return 0
    else
        log_error "TCP连接失败"
        SOCKET_FD=""
        return 1
    fi
}

# ===== 核心: 发送指令并等待完整响应 =====
# 设备每次只能执行一个指令, 本函数发送后阻塞等待完整的 <...> 响应
# 成功: 输出响应内容(不含尖括弧), 返回0
# 失败: 输出 TIMEOUT, 返回1
send_command() {
    local command=$1
    local timeout=${2:-2}

    if [[ -z "$SOCKET_FD" ]]; then
        echo "TIMEOUT"
        return 1
    fi

    # 丢弃缓冲区残留数据 (如上一条指令迟到的响应), 防止协议错位
    local line=""
    while IFS= read -t 0.1 -r line <&${SOCKET_FD} 2>/dev/null; do
        :
    done

    # 发送指令 (CRLF结尾)
    if ! printf '%s\r\n' "$command" >&${SOCKET_FD} 2>/dev/null; then
        echo "TIMEOUT"
        return 1
    fi

    # 等待完整响应 (累积读取直到出现完整的 <...> 块)
    local buffer=""
    local chunk=""
    local block_re='<([^>]*)>'
    local start now
    start=$(date +%s%N)

    while true; do
        # 优先从已有缓冲区中提取完整块
        if [[ "$buffer" =~ $block_re ]]; then
            local block="${BASH_REMATCH[1]}"
            # 若为设备回显的指令本身, 丢弃后继续等待真实响应
            if [[ "<$block>" == "$command" ]]; then
                buffer="${buffer#*<${block}>}"
                continue
            fi
            echo "$block"
            return 0
        fi

        now=$(date +%s%N)
        if (( now - start >= timeout * 1000000000 )); then
            break
        fi

        chunk=""
        local rc=0
        read -t 0.3 -r chunk <&${SOCKET_FD} 2>/dev/null || rc=$?
        # rc=1: 连接断开(EOF); rc>1: 超时(chunk中可能仍有部分数据)
        chunk="${chunk//$'\r'/}"
        buffer+="$chunk"

        if [[ $rc -eq 1 ]]; then
            break
        fi
    done

    echo "TIMEOUT"
    return 1
}

# 发送网口无返回的指令 (<RESET> / <RESTORE>, 成功后TCP连接自动断开)
send_no_response() {
    local command=$1
    if [[ -z "$SOCKET_FD" ]]; then
        log_error "未连接到设备"
        return 1
    fi
    if printf '%s\r\n' "$command" >&${SOCKET_FD} 2>/dev/null; then
        return 0
    fi
    log_error "发送命令失败"
    return 1
}

# ===== 光路状态操作 =====
# 解析 <OSW_A_?> 响应
# 说明书示例格式: OSW_01_02_..._16
# 实测设备格式:   OSW01_A_01_02_..._16 (前缀为 OSW01_A_)
# 健壮解析: 提取所有两位数字(00-16)取值, 取最后16个作为状态
parse_switch_state() {
    local resp=$1
    local IFS='_'
    local -a parts
    read -ra parts <<< "$resp"

    local -a vals=()
    local t
    for t in "${parts[@]}"; do
        if [[ "$t" =~ ^[0-9]{2}$ ]] && (( 10#$t <= 16 )); then
            vals+=("$t")
        fi
    done

    local n=${#vals[@]}
    local i idx
    for ((i=1; i<=16; i++)); do
        if (( n >= 16 )); then
            # 取最后16个值, 兼容不同前缀格式 (idx 范围 0..n-1)
            idx=$((n - 17 + i))
            SWITCH_STATE[i]="${vals[idx]}"
        else
            SWITCH_STATE[i]="00"
        fi
    done
}

# 查询并刷新本地光路状态
refresh_switch_state() {
    local result
    result=$(send_command "<OSW_A_?>" 3)
    if [[ "$result" == "TIMEOUT" || -z "$result" ]]; then
        log_error "查询光路状态失败 (无响应)"
        return 1
    fi
    if [[ "$result" == "ER" ]]; then
        log_error "查询光路状态失败, 设备返回 <ER>"
        return 1
    fi
    parse_switch_state "$result"
    return 0
}

# 显示当前光路状态
display_state() {
    local i v connected=0
    for ((i=1; i<=16; i++)); do
        if [[ "${SWITCH_STATE[i]}" != "00" ]]; then
            connected=$((connected+1))
        fi
    done
    echo "共 $connected/16 路已连接 (00=不通光):"
    for ((i=1; i<=16; i++)); do
        v="${SWITCH_STATE[i]}"
        if [[ "$v" == "00" ]]; then
            printf "  In%2d : 未连接\n" "$i"
        else
            printf "  In%2d : Out%s\n" "$i" "$v"
        fi
    done
}

# 根据当前 SWITCH_STATE 生成整路切换指令 <OSW_SW_v1_..._v16>
build_switch_command() {
    local cmd="OSW_SW"
    local i
    for ((i=1; i<=16; i++)); do
        cmd="${cmd}_${SWITCH_STATE[i]}"
    done
    echo "<${cmd}>"
}

# 检查状态表中是否存在输出端口冲突 (两个输入不能选择同一输出)
# 有冲突输出该冲突输出端口(两位), 否则无输出
check_conflict() {
    local i j v
    for ((i=1; i<=16; i++)); do
        v="${SWITCH_STATE[i]}"
        if [[ "$v" == "00" ]]; then
            continue
        fi
        for ((j=i+1; j<=16; j++)); do
            if [[ "${SWITCH_STATE[j]}" == "$v" ]]; then
                echo "$v"
                return 0
            fi
        done
    done
    return 0
}

# 按当前 SWITCH_STATE 发送切换指令
execute_switch() {
    local cmd result
    cmd=$(build_switch_command)
    log_info "发送切换指令: $cmd"
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "TIMEOUT" || -z "$result" ]]; then
        log_error "切换指令超时, 未收到响应"
        return 1
    fi
    if [[ "$result" == "ER" ]]; then
        log_error "非法命令! 设备返回 <ER> (可能两个输入选择了同一输出, 或取值超出00-16)"
        return 1
    fi
    if [[ "$result" == *OK* ]]; then
        return 0
    fi
    log_warn "收到未预期的响应: <$result>"
    return 1
}

# 重新查询并验证输入端口对应期望输出 (00=未连接)
verify_pair() {
    local input=$1 expected=$2
    local result
    result=$(send_command "<OSW_A_?>" 3)
    if [[ "$result" == "TIMEOUT" || -z "$result" || "$result" == "ER" ]]; then
        return 1
    fi
    parse_switch_state "$result"
    [[ "${SWITCH_STATE[input]}" == "$expected" ]]
}

# 查找未被占用的输出端口 (十进制), 无可用时输出为空
find_free_output() {
    local i j v
    for ((i=1; i<=16; i++)); do
        v=$(printf '%02d' "$i")
        for ((j=1; j<=16; j++)); do
            if [[ "${SWITCH_STATE[j]}" == "$v" ]]; then
                continue 2
            fi
        done
        echo "$i"
        return 0
    done
    return 1
}

# ===== 功能: 查询 =====
# 查询设备信息 <INFO_?>
# 成功返回: <OSW16X16-SM_VER1.00_SN01234567890_C06.02.00020>
query_device_info() {
    echo
    log_info "=== 设备信息 ==="
    local result
    result=$(send_command "<INFO_?>" 3)
    if [[ "$result" == "TIMEOUT" || -z "$result" ]]; then
        log_error "查询设备信息失败 (无响应)"
        return
    fi
    if [[ "$result" == "ER" ]]; then
        log_error "查询设备信息失败, 设备返回 <ER>"
        return
    fi
    echo "原始响应: <$result>"
    local IFS='_'
    local -a f
    read -ra f <<< "$result"
    if (( ${#f[@]} >= 4 )); then
        echo "型号:     ${f[0]}"
        echo "固件版本: ${f[1]#VER}"
        echo "SN号:     ${f[2]#SN}"
        echo "产品编号: ${f[3]}"
    fi
}

# 查询当前光路状态 <OSW_A_?>
query_state() {
    echo
    log_info "=== 当前光路状态 ==="
    if refresh_switch_state; then
        display_state
    fi
}

# ===== 功能: 单个连接/断开 =====
# 建立单个光路连接 (查询状态 -> 修改对应输入 -> 整路切换 -> 验证)
create_single_connection() {
    echo
    log_info "=== 建立光路连接 ==="
    echo "输入格式: 输入端口号,输出端口号 (1-16)"
    echo "示例: 1,3 (表示 In1 -> Out3)"

    local input_str input_port output_port
    while true; do
        echo -n "请输入连接参数 (或输入 q 退回菜单): "
        read -r input_str
        if [[ "$input_str" == "q" || "$input_str" == "Q" ]]; then
            log_info "退回主菜单..."
            return
        fi
        if [[ "$input_str" =~ ^[0-9]+,[0-9]+$ ]]; then
            IFS=',' read -r input_port output_port <<< "$input_str"
            input_port=$((10#$input_port))
            output_port=$((10#$output_port))
            if validate_optical_port "$input_port" && validate_optical_port "$output_port"; then
                break
            else
                log_error "端口号超出范围! 请输入1-16之间的端口号"
            fi
        else
            log_error "格式错误! 请输入格式: 输入端口,输出端口 (例如: 1,3)"
        fi
    done

    if ! refresh_switch_state; then
        return
    fi

    local padded_out old
    padded_out=$(printf '%02d' "$output_port")
    old="${SWITCH_STATE[input_port]}"
    SWITCH_STATE[input_port]="$padded_out"

    # 非法命令预检查: 两个输入不能选择同一输出
    local conflict
    conflict=$(check_conflict)
    if [[ -n "$conflict" ]]; then
        log_error "非法命令: 输出端口 $((10#$conflict)) 已被其他输入端口占用, 操作取消"
        SWITCH_STATE[input_port]="$old"
        return
    fi

    echo
    log_info "建立连接: In$input_port -> Out$output_port"
    if execute_switch; then
        if verify_pair "$input_port" "$padded_out"; then
            log_success "连接已成功建立并验证: In$input_port -> Out$output_port"
        else
            log_warn "切换成功但状态验证不一致, 请重新查询光路状态"
        fi
    else
        log_error "连接建立失败"
        refresh_switch_state
    fi
}

# 断开单个光路连接
remove_single_connection() {
    echo
    log_info "=== 断开光路连接 ==="
    echo "输入格式: 输入端口号,输出端口号 (1-16)"
    echo "示例: 1,3 (表示断开 In1 -> Out3)"

    local input_str input_port output_port
    while true; do
        echo -n "请输入断开参数 (或输入 q 退回菜单): "
        read -r input_str
        if [[ "$input_str" == "q" || "$input_str" == "Q" ]]; then
            log_info "退回主菜单..."
            return
        fi
        if [[ "$input_str" =~ ^[0-9]+,[0-9]+$ ]]; then
            IFS=',' read -r input_port output_port <<< "$input_str"
            input_port=$((10#$input_port))
            output_port=$((10#$output_port))
            if validate_optical_port "$input_port" && validate_optical_port "$output_port"; then
                break
            else
                log_error "端口号超出范围! 请输入1-16之间的端口号"
            fi
        else
            log_error "格式错误! 请输入格式: 输入端口,输出端口 (例如: 1,3)"
        fi
    done

    if ! refresh_switch_state; then
        return
    fi

    local padded_out current
    padded_out=$(printf '%02d' "$output_port")
    current="${SWITCH_STATE[input_port]}"

    if [[ "$current" != "$padded_out" ]]; then
        if [[ "$current" == "00" ]]; then
            log_error "输入端口 $input_port 当前未连接, 无需断开"
        else
            log_error "输入端口 $input_port 当前连接的是 Out$((10#$current)), 不是 Out$output_port"
        fi
        return
    fi

    SWITCH_STATE[input_port]="00"

    echo
    log_info "断开连接: In$input_port -> Out$output_port"
    if execute_switch; then
        if verify_pair "$input_port" "00"; then
            log_success "连接已成功断开并验证: In$input_port"
        else
            log_warn "切换成功但状态验证不一致, 请重新查询光路状态"
        fi
    else
        log_error "连接断开失败"
        refresh_switch_state
    fi
}

# ===== 功能: 批量连接/断开 =====
# 解析批量输入: input1,output1;input2,output2;...
# 结果存入全局数组 BATCH_INPUTS[] / BATCH_OUTPUTS[] (十进制)
parse_batch_input() {
    local input=$1
    BATCH_INPUTS=()
    BATCH_OUTPUTS=()

    if [[ -z "$input" ]]; then
        return 1
    fi

    local IFS=';'
    local -a pairs
    read -ra pairs <<< "$input"

    local pair ip op k
    for pair in "${pairs[@]}"; do
        pair="${pair// /}"
        if [[ -z "$pair" ]]; then
            continue
        fi
        if [[ ! "$pair" =~ ^[0-9]+,[0-9]+$ ]]; then
            return 1
        fi
        IFS=',' read -r ip op <<< "$pair"
        ip=$((10#$ip))
        op=$((10#$op))
        if ! validate_optical_port "$ip" || ! validate_optical_port "$op"; then
            return 1
        fi
        # 同一批中输入端口不可重复
        for ((k=0; k<${#BATCH_INPUTS[@]}; k++)); do
            if [[ "${BATCH_INPUTS[k]}" == "$ip" ]]; then
                return 1
            fi
        done
        BATCH_INPUTS+=("$ip")
        BATCH_OUTPUTS+=("$op")
    done

    [[ ${#BATCH_INPUTS[@]} -gt 0 ]]
}

# 根据用户输入的部分连接关系, 自动补全全部16路
# 规则: 以恒等映射(In_i->Out_i)为基准, 依次应用用户指定的交叉连接;
#       通过"交换两个输入的目标输出"保持排列合法,
#       效果: 用户指定的交叉连接形成轮换, 其余端口保持一一对应
# 例: 输入 1,2;3,4 -> In1->Out2, In2->Out1, In3->Out4, In4->Out3, In5->Out5, ... In16->Out16
auto_complete_state() {
    local n=${#BATCH_INPUTS[@]}
    local i j a_in a_out a b

    # 初始化为恒等映射
    for ((i=1; i<=16; i++)); do
        SWITCH_STATE[i]=$(printf '%02d' "$i")
    done

    # 依次应用用户指定的连接关系
    for ((i=0; i<n; i++)); do
        a_in=${BATCH_INPUTS[i]}
        a_out=$(printf '%02d' "${BATCH_OUTPUTS[i]}")
        a="${SWITCH_STATE[a_in]}"
        # 找当前指向输出 a_out 的输入端口 b
        b=0
        for ((j=1; j<=16; j++)); do
            if [[ "${SWITCH_STATE[j]}" == "$a_out" ]]; then
                b=$j
                break
            fi
        done
        # 交换两个输入的目标输出: a_in -> a_out, b -> a
        if [[ "$a_in" != "$b" ]]; then
            SWITCH_STATE[a_in]="$a_out"
            SWITCH_STATE[b]="$a"
        fi
    done
}

# 批量建立连接 (自动补全其余端口)
add_multiple_links() {
    echo
    log_info "=== 批量建立连接 (自动补全) ==="
    echo "输入格式: input1,output1;input2,output2;..."
    echo "只需输入需要交叉连接的端口对, 其余端口自动按一一对应(In_i->Out_i)补全"
    echo "示例: 1,2;3,4 (结果: In1->Out2, In2->Out1, In3->Out4, In4->Out3, 其余一一对应)"

    local input_str
    while true; do
        echo -n "请输入连接参数 (或输入 q 退回菜单): "
        read -r input_str
        if [[ "$input_str" == "q" || "$input_str" == "Q" ]]; then
            log_info "退回主菜单..."
            return
        fi
        if parse_batch_input "$input_str"; then
            break
        else
            log_error "格式错误! 请重新输入 (端口1-16, 输入端口不可重复)"
        fi
    done

    # 检查输出端口是否重复 (两个输入不能选择同一输出)
    local n=${#BATCH_INPUTS[@]}
    local i j k
    for ((i=0; i<n; i++)); do
        for ((j=i+1; j<n; j++)); do
            if [[ "${BATCH_OUTPUTS[i]}" == "${BATCH_OUTPUTS[j]}" ]]; then
                log_error "格式错误: 输出端口 $((10#${BATCH_OUTPUTS[i]})) 被多个输入端口选择"
                return
            fi
        done
    done

    # 自动补全全部16路
    auto_complete_state

    log_info "自动补全后的完整连接关系 ([指定] 为输入项):"
    local marker
    for ((i=1; i<=16; i++)); do
        marker=""
        for ((k=0; k<n; k++)); do
            if [[ "${BATCH_INPUTS[k]}" == "$i" ]]; then
                marker=" [指定]"
                break
            fi
        done
        echo "  In$(printf '%2d' "$i") -> Out${SWITCH_STATE[i]}${marker}"
    done

    # 保存目标状态, 供切换后验证
    local -a target_state=()
    for ((i=1; i<=16; i++)); do
        target_state[i]="${SWITCH_STATE[i]}"
    done

    # 非法命令预检查 (自动补全后理论上不会冲突, 保险起见)
    local conflict
    conflict=$(check_conflict)
    if [[ -n "$conflict" ]]; then
        log_error "非法命令: 输出端口 $((10#$conflict)) 被多个输入端口选择, 操作取消"
        return
    fi

    log_info "即将执行批量连接 (全部16路)"
    if execute_switch; then
        local verify ok_count=0
        verify=$(send_command "<OSW_A_?>" 3)
        if [[ "$verify" != "TIMEOUT" && -n "$verify" && "$verify" != "ER" ]]; then
            parse_switch_state "$verify"
            for ((i=1; i<=16; i++)); do
                if [[ "${SWITCH_STATE[i]}" == "${target_state[i]}" ]]; then
                    ok_count=$((ok_count+1))
                fi
            done
            if [[ $ok_count -eq 16 ]]; then
                log_success "批量连接(含自动补全)成功并验证 (16/16)"
            else
                log_warn "切换成功但仅验证 $ok_count/16 项, 请重新查询光路状态"
            fi
        else
            log_warn "切换成功但状态验证失败"
        fi
    else
        log_error "批量连接失败"
        refresh_switch_state
    fi
}

# 批量断开连接
remove_multiple_links() {
    echo
    log_info "=== 批量断开连接 ==="
    echo "输入格式: input1,output1;input2,output2;..."
    echo "端口范围1-16, 示例: 1,3;2,4"

    local input_str
    while true; do
        echo -n "请输入批量断开参数 (或输入 q 退回菜单): "
        read -r input_str
        if [[ "$input_str" == "q" || "$input_str" == "Q" ]]; then
            log_info "退回主菜单..."
            return
        fi
        if parse_batch_input "$input_str"; then
            break
        else
            log_error "格式错误! 请重新输入 (端口1-16, 输入端口不可重复)"
        fi
    done

    if ! refresh_switch_state; then
        return
    fi

    local n=${#BATCH_INPUTS[@]}
    local i ip padded_out current

    # 逐项校验当前状态
    for ((i=0; i<n; i++)); do
        ip=${BATCH_INPUTS[i]}
        padded_out=$(printf '%02d' "${BATCH_OUTPUTS[i]}")
        current="${SWITCH_STATE[ip]}"
        if [[ "$current" != "$padded_out" ]]; then
            if [[ "$current" == "00" ]]; then
                log_error "输入端口 $ip 当前未连接, 无法断开 In$ip -> Out$((10#$padded_out))"
            else
                log_error "输入端口 $ip 当前连接的是 Out$((10#$current)), 不是 Out$((10#$padded_out))"
            fi
            return
        fi
        SWITCH_STATE[ip]="00"
    done

    log_info "即将执行批量断开 (共 $n 项):"
    for ((i=0; i<n; i++)); do
        echo "  In${BATCH_INPUTS[i]} -> (断开)"
    done

    if execute_switch; then
        local verify ok_count=0
        verify=$(send_command "<OSW_A_?>" 3)
        if [[ "$verify" != "TIMEOUT" && -n "$verify" && "$verify" != "ER" ]]; then
            parse_switch_state "$verify"
            for ((i=0; i<n; i++)); do
                if [[ "${SWITCH_STATE[${BATCH_INPUTS[i]}]}" == "00" ]]; then
                    ok_count=$((ok_count+1))
                fi
            done
            if [[ $ok_count -eq $n ]]; then
                log_success "批量断开成功并验证 ($ok_count/$n)"
            else
                log_warn "切换成功但仅验证 $ok_count/$n 项, 请重新查询光路状态"
            fi
        else
            log_warn "切换成功但状态验证失败"
        fi
    else
        log_error "批量断开失败"
        refresh_switch_state
    fi
}

# 断开所有连接 (16路全部置00)
disconnect_all_links() {
    echo
    log_info "=== 断开所有连接 ==="
    local i
    for ((i=1; i<=16; i++)); do
        if [[ "${SWITCH_STATE[i]}" != "00" ]]; then
            log_warn "将断开: In$i -> Out$((10#${SWITCH_STATE[i]}))"
        fi
    done
    for ((i=1; i<=16; i++)); do
        SWITCH_STATE[i]="00"
    done

    if execute_switch; then
        local verify
        verify=$(send_command "<OSW_A_?>" 3)
        if [[ "$verify" != "TIMEOUT" && -n "$verify" && "$verify" != "ER" ]]; then
            parse_switch_state "$verify"
            local all_off=1
            for ((i=1; i<=16; i++)); do
                if [[ "${SWITCH_STATE[i]}" != "00" ]]; then
                    all_off=0
                fi
            done
            if [[ $all_off -eq 1 ]]; then
                log_success "所有连接已成功断开并验证"
            else
                log_warn "命令成功但仍有通道处于连接状态, 请重新查询确认"
            fi
        else
            log_warn "命令发送成功, 但状态验证失败"
        fi
    else
        log_error "断开所有连接失败"
        refresh_switch_state
    fi
}

# ===== 功能: 网络设置 =====
# 将 192_168_002_011 格式化为 192.168.2.11
format_dotted() {
    local s=$1
    local IFS='_'
    local -a o
    read -ra o <<< "$s"
    local out="" x
    for x in "${o[@]}"; do
        if [[ -n "$out" ]]; then
            out+="."
        fi
        out+=$((10#$x))
    done
    printf '%s' "$out"
}

# 查询全部网络配置
query_network_info() {
    echo
    log_info "=== 网络配置查询 ==="
    local r
    r=$(send_command "<IP_?>" 3)
    if [[ "$r" != "TIMEOUT" && -n "$r" && "$r" != "ER" && "$r" == IP_* ]]; then
        echo "设备IP:   $(format_dotted "${r#IP_}")"
    elif [[ "$r" == "ER" ]]; then
        echo "设备IP:   设备不支持该查询 (返回<ER>)"
    else
        echo "设备IP:   查询失败"
    fi
    r=$(send_command "<GW_?>" 3)
    if [[ "$r" != "TIMEOUT" && -n "$r" && "$r" != "ER" && "$r" == GW_* ]]; then
        echo "网关:     $(format_dotted "${r#GW_}")"
    elif [[ "$r" == "ER" ]]; then
        echo "网关:     设备不支持该查询 (返回<ER>)"
    else
        echo "网关:     查询失败"
    fi
    r=$(send_command "<SM_?>" 3)
    if [[ "$r" != "TIMEOUT" && -n "$r" && "$r" != "ER" && "$r" == SM_* ]]; then
        echo "子网掩码: $(format_dotted "${r#SM_}")"
    elif [[ "$r" == "ER" ]]; then
        echo "子网掩码: 设备不支持该查询 (返回<ER>)"
    else
        echo "子网掩码: 查询失败"
    fi
    r=$(send_command "<TCPP_?>" 3)
    if [[ "$r" != "TIMEOUT" && -n "$r" && "$r" != "ER" && "$r" == TCPP_* ]]; then
        echo "TCP端口:  $((10#${r#TCPP_}))"
    elif [[ "$r" == "ER" ]]; then
        echo "TCP端口:  设备不支持该查询 (返回<ER>)"
    else
        echo "TCP端口:  查询失败"
    fi
}

# 设置IP地址 (重启生效)
set_ip() {
    echo
    log_info "=== 设置设备IP地址 (重启后生效) ==="
    echo -n "请输入新IP地址 (xxx.xxx.xxx.xxx): "
    local ip a b c d
    read -r ip
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        log_error "IP格式错误"
        return
    fi
    IFS='.' read -r a b c d <<< "$ip"
    a=$((10#$a)); b=$((10#$b)); c=$((10#$c)); d=$((10#$d))
    local x
    for x in $a $b $c $d; do
        if (( x < 0 || x > 255 )); then
            log_error "IP段必须为0-255"
            return
        fi
    done
    local cmd
    cmd=$(printf '<SET_IP_%03d_%03d_%03d_%03d>' $a $b $c $d)
    log_info "发送指令: $cmd"
    local result
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "SET_IP_OK" ]]; then
        log_success "IP地址设置成功 (重启后生效): $a.$b.$c.$d"
        log_warn "注意: 重启后请使用新IP地址重新连接"
    elif [[ "$result" == "ER" ]]; then
        log_error "设备返回 <ER>: 设置失败"
    else
        log_warn "未预期的响应: $result"
    fi
}

# 设置网关 (重启生效)
set_gw() {
    echo
    log_info "=== 设置设备网关 (重启后生效) ==="
    echo -n "请输入新网关地址 (xxx.xxx.xxx.xxx): "
    local ip a b c d
    read -r ip
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        log_error "网关格式错误"
        return
    fi
    IFS='.' read -r a b c d <<< "$ip"
    a=$((10#$a)); b=$((10#$b)); c=$((10#$c)); d=$((10#$d))
    local x
    for x in $a $b $c $d; do
        if (( x < 0 || x > 255 )); then
            log_error "网关段必须为0-255"
            return
        fi
    done
    local cmd
    cmd=$(printf '<SET_GW_%03d_%03d_%03d_%03d>' $a $b $c $d)
    log_info "发送指令: $cmd"
    local result
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "SET_GW_OK" ]]; then
        log_success "网关设置成功 (重启后生效): $a.$b.$c.$d"
    elif [[ "$result" == "ER" ]]; then
        log_error "设备返回 <ER>: 设置失败"
    else
        log_warn "未预期的响应: $result"
    fi
}

# 设置子网掩码 (重启生效)
set_sm() {
    echo
    log_info "=== 设置设备子网掩码 (重启后生效) ==="
    echo -n "请输入新子网掩码 (xxx.xxx.xxx.xxx): "
    local ip a b c d
    read -r ip
    if [[ ! "$ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        log_error "子网掩码格式错误"
        return
    fi
    IFS='.' read -r a b c d <<< "$ip"
    a=$((10#$a)); b=$((10#$b)); c=$((10#$c)); d=$((10#$d))
    local x
    for x in $a $b $c $d; do
        if (( x < 0 || x > 255 )); then
            log_error "掩码段必须为0-255"
            return
        fi
    done
    local cmd
    cmd=$(printf '<SET_SM_%03d_%03d_%03d_%03d>' $a $b $c $d)
    log_info "发送指令: $cmd"
    local result
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "SET_SM_OK" ]]; then
        log_success "子网掩码设置成功 (重启后生效): $a.$b.$c.$d"
    elif [[ "$result" == "ER" ]]; then
        log_error "设备返回 <ER>: 设置失败"
    else
        log_warn "未预期的响应: $result"
    fi
}

# 设置TCP端口 (重启生效)
set_tcpport() {
    echo
    log_info "=== 设置设备TCP端口 (重启后生效) ==="
    echo -n "请输入新TCP端口 (1-65534): "
    local p
    read -r p
    if [[ ! "$p" =~ ^[0-9]+$ ]] || (( 10#$p < 1 || 10#$p > 65534 )); then
        log_error "端口号错误 (1-65534)"
        return
    fi
    p=$((10#$p))
    local cmd
    cmd=$(printf '<SET_TCPP_%05d>' $p)
    log_info "发送指令: $cmd"
    local result
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "SET_TCPP_OK" ]]; then
        log_success "TCP端口设置成功 (重启后生效): $p"
        log_warn "注意: 重启后请使用新端口重新连接"
    elif [[ "$result" == "ER" ]]; then
        log_error "设备返回 <ER>: 设置失败"
    else
        log_warn "未预期的响应: $result"
    fi
}

# 网络设置子菜单
network_menu() {
    local choice
    while true; do
        echo
        echo "=== 网络设置 ==="
        echo "1. 查询全部网络配置 (IP/网关/子网掩码/TCP端口)"
        echo "2. 设置IP地址 (重启生效)"
        echo "3. 设置网关 (重启生效)"
        echo "4. 设置子网掩码 (重启生效)"
        echo "5. 设置TCP端口 (重启生效)"
        echo "0. 返回主菜单"
        echo -n "请选择: "
        read -r choice
        case "$choice" in
            1) query_network_info ;;
            2) set_ip ;;
            3) set_gw ;;
            4) set_sm ;;
            5) set_tcpport ;;
            0) return ;;
            *) log_error "无效选择" ;;
        esac
        if [[ "$choice" != "0" ]]; then
            echo -n "按回车键继续..."
            read -r _
        fi
    done
}

# ===== 功能: 面板按键权限 =====
query_key() {
    local r
    r=$(send_command "<KEY_?>" 3)
    if [[ "$r" == "KEY_1" ]]; then
        echo "面板按键: 允许使用"
    elif [[ "$r" == "KEY_0" ]]; then
        echo "面板按键: 禁止使用 (已锁定)"
    elif [[ "$r" == "ER" ]]; then
        log_error "查询失败, 设备返回 <ER>"
    else
        echo "响应: <$r>"
    fi
}

set_key() {
    local x=$1
    local cmd="<SET_KEY_${x}>"
    log_info "发送指令: $cmd"
    local result
    result=$(send_command "$cmd" 3)
    if [[ "$result" == "SET_KEY_${x}_OK" ]]; then
        if [[ $x -eq 1 ]]; then
            log_success "面板按键已允许使用"
        else
            log_success "面板按键已禁止使用 (已锁定)"
        fi
    elif [[ "$result" == "ER" ]]; then
        log_error "设备返回 <ER>: 设置失败"
    else
        log_warn "未预期的响应: $result"
    fi
}

# 面板按键权限子菜单
key_menu() {
    local choice
    while true; do
        echo
        echo "=== 面板按键权限 ==="
        echo "1. 查询按键权限 (<KEY_?>)"
        echo "2. 允许面板按键使用 (<SET_KEY_1>)"
        echo "3. 禁止面板按键使用 (<SET_KEY_0>)"
        echo "0. 返回主菜单"
        echo -n "请选择: "
        read -r choice
        case "$choice" in
            1) query_key ;;
            2) set_key 1 ;;
            3) set_key 0 ;;
            0) return ;;
            *) log_error "无效选择" ;;
        esac
        if [[ "$choice" != "0" ]]; then
            echo -n "按回车键继续..."
            read -r _
        fi
    done
}

# ===== 功能: 自定义指令 =====
custom_command() {
    echo
    log_info "=== 自定义指令 ==="
    echo "指令需使用 < > 包裹且为大写, 例如: <OSW_A_?>、<IP_?>"
    echo -n "输入指令 (或输入 q 退回菜单): "
    local command
    read -r command
    if [[ "$command" == "q" || "$command" == "Q" || -z "$command" ]]; then
        log_info "退回主菜单..."
        return
    fi

    command="${command^^}"
    if [[ "$command" != "<"* ]]; then
        command="<$command"
    fi
    if [[ "$command" != *">" ]]; then
        command="${command}>"
    fi

    # <RESET> / <RESTORE> 网口无返回, 成功后TCP连接自动断开
    if [[ "$command" == "<RESET>" || "$command" == "<RESTORE>" ]]; then
        if confirm "指令 $command 将导致设备重启/恢复出厂, 网口无返回且TCP连接将自动断开。继续? (y/N)"; then
            if send_no_response "$command"; then
                log_warn "指令已发送, 设备将重启/恢复, TCP连接已断开, 请稍后重新连接"
                cleanup
            fi
        else
            log_info "已取消"
        fi
        return
    fi

    local result
    result=$(send_command "$command" 3)
    if [[ "$result" != "TIMEOUT" && -n "$result" ]]; then
        echo "响应: <$result>"
    else
        log_error "指令超时, 未收到响应"
    fi
}

# ===== 功能: 测试连接功能 =====
test_connection_function() {
    echo
    log_info "=== 测试连接功能 ==="
    echo "测试内容: 断开 In1 并重新连接回原输出端口, 以验证切换功能"

    if ! refresh_switch_state; then
        return
    fi

    local orig="${SWITCH_STATE[1]}"
    local target
    if [[ "$orig" != "00" ]]; then
        target="$orig"
    else
        target=$(find_free_output)
        if [[ -z "$target" ]]; then
            log_error "16个输出端口全部占用, 无可用输出端口进行测试"
            return
        fi
        target=$(printf '%02d' "$target")
    fi

    if [[ "$orig" == "00" ]]; then
        echo "In1 当前: 未连接 | 测试目标输出端口: Out$target"
    else
        echo "In1 当前: Out$orig | 测试目标输出端口: Out$target"
    fi

    # 步骤1: 断开 In1
    log_info "步骤1: 断开 In1"
    SWITCH_STATE[1]="00"
    if ! execute_switch; then
        log_error "测试失败: 无法断开 In1"
        refresh_switch_state
        return
    fi
    if verify_pair 1 "00"; then
        log_success "In1 已断开"
    else
        log_warn "In1 断开后验证失败"
    fi

    # 步骤2: 重新连接 In1 -> 目标输出
    log_info "步骤2: 重新连接 In1 -> Out$target"
    SWITCH_STATE[1]="$target"
    if execute_switch; then
        if verify_pair 1 "$target"; then
            log_success "In1 已连接 Out$target"
        else
            log_warn "In1 连接后验证失败"
        fi
    else
        log_error "测试失败: 无法重连 In1"
        refresh_switch_state
        return
    fi

    # 步骤3: 查询最终状态
    log_info "步骤3: 查询最终状态"
    if refresh_switch_state; then
        display_state
    fi
    log_info "连接功能测试完成 (In1 已恢复)"
}

# ===== 隐藏功能 (代码保留, 未列入菜单; 如需使用可在 show_menu 与 case 中重新添加) =====
# 重启设备 (<RESET>)
restart_device() {
    if confirm "即将重启设备 (<RESET>), 网口无返回, 成功后TCP连接自动断开。继续? (y/N)"; then
        if send_no_response "<RESET>"; then
            log_warn "重启指令已发送, 设备正在重启, 请稍后重新连接"
            cleanup
        fi
    else
        log_info "已取消"
    fi
}

# 恢复出厂设置 (<RESTORE>, 仅恢复网络参数)
restore_factory() {
    if confirm "即将恢复出厂设置 (<RESTORE>, 仅恢复网络参数为默认值), 成功后TCP连接自动断开。继续? (y/N)"; then
        if send_no_response "<RESTORE>"; then
            log_warn "恢复指令已发送, 网络参数将恢复为出厂默认 (IP 192.168.1.178, 端口 4001), 请稍后重新连接"
            cleanup
        fi
    else
        log_info "已取消"
    fi
}

# ===== 菜单 =====
show_menu() {
    echo
    echo "=== 16X16 矩阵光开关控制菜单 (OSW协议) ==="
    if [[ -z "$SOCKET_FD" ]]; then
        echo "状态: 未连接 (请选择 9 重新连接)"
    else
        echo "状态: 已连接 $SWITCH_IP:$SWITCH_PORT"
    fi
    echo "1.  查询设备信息 (<INFO_?>)"
    echo "2.  查询当前光路状态 (<OSW_A_?>)"
    echo "3.  建立光路连接"
    echo "4.  断开光路连接"
    echo "5.  批量建立连接 (自动补全其余端口)"
    echo "6.  批量断开连接"
    echo "7.  断开所有连接"
    echo "8.  测试连接功能"
    echo "9.  重新连接设备"
    echo "0.  退出"
    echo -n "请选择: "
}

# 主运行循环
run_controller() {
    local choice retry
    while true; do
        show_menu
        read -r choice

        case "$choice" in
            1) query_device_info ;;
            2) query_state ;;
            3) create_single_connection ;;
            4) remove_single_connection ;;
            5) add_multiple_links ;;
            6) remove_multiple_links ;;
            7) disconnect_all_links ;;
            8) test_connection_function ;;
            9)
                log_info "重新连接设备 $SWITCH_IP:$SWITCH_PORT ..."
                cleanup
                retry=0
                while ! connect_switch && (( retry < 3 )); do
                    retry=$((retry+1))
                    log_warn "重新连接失败 ($retry/3), 3秒后重试..."
                    sleep 3
                done
                if (( retry >= 3 )); then
                    log_error "重新连接失败, 请检查网络或设备"
                else
                    refresh_switch_state
                fi
                ;;
            0)
                log_info "退出程序"
                exit 0
                ;;
            *)
                log_error "无效选择, 请输入0-9的数字"
                ;;
        esac

        # 暂停以便用户查看结果
        echo -n "按回车键继续..."
        read -r _
    done
}

# 检查依赖
check_dependencies() {
    local deps=("nc" "bash")
    local missing=()
    local dep
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" >/dev/null 2>&1; then
            missing+=("$dep")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        log_error "缺少以下依赖: ${missing[*]}"
        echo "请运行以下命令安装:"
        echo "sudo apt-get update && sudo apt-get install netcat-openbsd"
        exit 1
    fi
}

# 主函数
main() {
    echo "16X16 矩阵光开关控制脚本 (OXC-16X16-1U-LP)"
    echo "========================"
    echo "设备配置: $SWITCH_IP:$SWITCH_PORT (修改请编辑脚本顶部配置区)"

    # 检查依赖
    check_dependencies

    # 使用配置区的IP/端口连接 (失败自动重试, 最多3次)
    local retry_count=0
    while ! connect_switch; do
        retry_count=$((retry_count+1))
        if (( retry_count >= 3 )); then
            log_error "连接失败次数过多, 程序退出 (请检查设备/网络, 或修改脚本顶部配置区)"
            exit 1
        fi
        log_warn "连接失败 (尝试 $retry_count/3), 3秒后重试..."
        sleep 3
    done

    # 初始同步光路状态
    refresh_switch_state || log_warn "初始化光路状态失败 (不影响其他功能)"

    # 运行控制器
    run_controller
}

# 执行主函数
main "$@"
