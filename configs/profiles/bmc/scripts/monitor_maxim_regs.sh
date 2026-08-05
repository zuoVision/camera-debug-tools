#!/bin/sh

# ==============================================================================
# Chery GMSL & PMIC Real-Time Monitor (Pure Shell - Bulk Read & Card UI v1.4)
# ==============================================================================

INTERVAL=1
if [ -n "$1" ]; then
    INTERVAL=$1
fi

# Define temp directory
TMP_DIR="/tmp/gmsl_monitor"
mkdir -p "$TMP_DIR"

# Clean up temp files on exit
cleanup() {
    rm -rf "$TMP_DIR"
    printf "%b" "\033[?25h" # Show cursor
    exit 0
}
trap cleanup INT TERM EXIT

# Hide cursor
printf "%b" "\033[?25l"

# Setup board csi tool shared libraries
export LD_LIBRARY_PATH=/bmc/share:/bmc/lib:/usr/lib:$LD_LIBRARY_PATH

# Global constant for carriage return
CR=$(printf '\r')

# Optimized bulk register queries
collect_g0() {
    # 1. PMIC (alen=1, count=1)
    /bmc/bin/csi i2c 0 0 0x50,1 0x01,1 2>/dev/null
    # 2. Deserializer Links (alen=2, count=17 to read 0x0A ~ 0x1A)
    /bmc/bin/csi i2c 0 0 0x4E,2 0x0A,17 2>/dev/null
    # 3. Deserializer Video Lock Part 1 (alen=2, count=33 to read 0x1DC ~ 0x1FC)
    /bmc/bin/csi i2c 0 0 0x4E,2 0x1DC,33 2>/dev/null
    # 4. Deserializer Video Lock Part 2 (alen=2, count=33 to read 0x21C ~ 0x23C)
    /bmc/bin/csi i2c 0 0 0x4E,2 0x21C,33 2>/dev/null
}

collect_g1() {
    local cnt=$1
    /bmc/bin/csi i2c 1 1 0x50,1 0x01,1 2>/dev/null
    /bmc/bin/csi i2c 1 1 0x52,2 0x0A,17 2>/dev/null
    /bmc/bin/csi i2c 1 1 0x52,2 0x1DC,33 2>/dev/null
    /bmc/bin/csi i2c 1 1 0x52,2 0x21C,33 2>/dev/null
    
    # Decouple and read high-cost bypass links every 3 cycles to reduce single bus I2C load
    if [ "$cnt" -eq 0 ]; then
        echo "--- BYPASS START ---"
        echo "F2_13:"
        /bmc/bin/csi i2c 1 1 0xf2,2 0x0013,1 2>/dev/null
        echo "F2_112:"
        /bmc/bin/csi i2c 1 1 0xf2,2 0x0112,1 2>/dev/null
        echo "F4_13:"
        /bmc/bin/csi i2c 1 1 0xf4,2 0x0013,1 2>/dev/null
        echo "F4_112:"
        /bmc/bin/csi i2c 1 1 0xf4,2 0x0112,1 2>/dev/null
    fi
}

collect_g2() {
    /bmc/bin/csi i2c 2 2 0x50,1 0x01,1 2>/dev/null
    /bmc/bin/csi i2c 2 2 0xD6,2 0x0A,17 2>/dev/null
    /bmc/bin/csi i2c 2 2 0xD6,2 0x1DC,33 2>/dev/null
    /bmc/bin/csi i2c 2 2 0xD6,2 0x21C,33 2>/dev/null
}

# PMIC status formatter (Displays P0-P3 camera power status individually)
format_pmic_ports() {
    local val=$1
    local RED="\033[91m"
    local GREEN="\033[92m"
    local RESET="\033[0m"
    local GRAY="\033[90m"
    
    if [ "$val" = "ERR" ] || [ -z "$val" ]; then
        printf "${GRAY}P0:${RED}ERR  ${GRAY}P1:${RED}ERR  ${GRAY}P2:${RED}ERR  ${GRAY}P3:${RED}ERR${RESET}"
        return
    fi
    
    local dec=$((val))
    local p0=$(( dec & 0x01 ))
    local p1=$(( (dec & 0x02) >> 1 ))
    local p2=$(( (dec & 0x04) >> 2 ))
    local p3=$(( (dec & 0x08) >> 3 ))
    
    local p0_str="${GRAY}P0:${RED}OFF${RESET}"
    [ $p0 -eq 1 ] && p0_str="${GRAY}P0:${GREEN}ON ${RESET}"
    
    local p1_str="${GRAY}P1:${RED}OFF${RESET}"
    [ $p1 -eq 1 ] && p1_str="${GRAY}P1:${GREEN}ON ${RESET}"
    
    local p2_str="${GRAY}P2:${RED}OFF${RESET}"
    [ $p2 -eq 1 ] && p2_str="${GRAY}P2:${GREEN}ON ${RESET}"
    
    local p3_str="${GRAY}P3:${RED}OFF${RESET}"
    [ $p3 -eq 1 ] && p3_str="${GRAY}P3:${GREEN}ON ${RESET}"
    
    printf "%b  %b  %b  %b" "$p0_str" "$p1_str" "$p2_str" "$p3_str"
}

# Deserializer Port Link & Video combined formatter
format_port_status() {
    local link_reg_val=$1
    local video_reg_val=$2
    
    local RED="\033[91m"
    local GREEN="\033[92m"
    local YELLOW="\033[93m"
    local RESET="\033[0m"
    local GRAY="\033[90m"
    
    if [ "$link_reg_val" = "ERR" ] || [ -z "$link_reg_val" ] || [ "$video_reg_val" = "ERR" ] || [ -z "$video_reg_val" ]; then
        printf "${RED}ERR / ERR${RESET}"
        return
    fi
    
    local link_dec=$((link_reg_val))
    local link_lock=$(( (link_dec & 0x08) >> 3 ))
    
    local video_dec=$((video_reg_val))
    local video_lock=$(( video_dec & 0x01 ))
    
    local link_str="${RED}DISC${RESET}"
    [ $link_lock -eq 1 ] && link_str="${GREEN}LKD${RESET}"
    
    local video_str="${YELLOW}NV${RESET}"
    [ $video_lock -eq 1 ] && video_str="${GREEN}OK${RESET}"
    
    printf "[ %b ${GRAY}/ ${RESET}%b ]" "$link_str" "$video_str"
}

# Bypass Link & PCLK combined formatter
format_bypass_status() {
    local link_reg_val=$1
    local pclk_reg_val=$2
    
    local RED="\033[91m"
    local GREEN="\033[92m"
    local YELLOW="\033[93m"
    local RESET="\033[0m"
    local GRAY="\033[90m"
    
    if [ "$link_reg_val" = "ERR" ] || [ -z "$link_reg_val" ] || [ "$pclk_reg_val" = "ERR" ] || [ -z "$pclk_reg_val" ]; then
        printf "${RED}ERR / ERR${RESET}"
        return
    fi
    
    local link_dec=$((link_reg_val))
    local link_lock=$(( (link_dec & 0x08) >> 3 ))
    
    local pclk_dec=$((pclk_reg_val))
    local pclk_det=$(( (pclk_dec & 0x80) >> 7 ))
    
    local link_str="${RED}DISC${RESET}"
    [ $link_lock -eq 1 ] && link_str="${GREEN}LKD${RESET}"
    
    local pclk_str="${YELLOW}NV${RESET}"
    [ $pclk_det -eq 1 ] && pclk_str="${GREEN}OK${RESET}"
    
    printf "[ %b ${GRAY}/ ${RESET}%b ]" "$link_str" "$pclk_str"
}

# Clear terminal screen once at startup
printf "%b" "\033[2J\033[H"

# Card layout borders (Width 72)
BORDER_TOP="┌──────────────────────────────────────────────────────────────────────┐"
BORDER_MID="├──────────────────────────────────────────────────────────────────────┤"
BORDER_BOT="└──────────────────────────────────────────────────────────────────────┘"
BORDER_COLOR="\033[36m" # Cyan border
RESET_COLOR="\033[0m"

# Initialize long-lived bypass variables outside loop to support memory/persist rendering
bp_f2_13="ERR"
bp_f2_112="ERR"
bp_f4_13="ERR"
bp_f4_112="ERR"

# Make sure count tracker is initialized
echo "0" > "$TMP_DIR/cnt"

while true; do
    # Read count tracker, calculate next state
    CURR_CNT=0
    [ -f "$TMP_DIR/cnt" ] && CURR_CNT=$(cat "$TMP_DIR/cnt")
    NEXT_CNT=$(( (CURR_CNT + 1) % 3 ))
    echo "$NEXT_CNT" > "$TMP_DIR/cnt"

    # 1. Collect registers in parallel across cores/buses, output redirected to 3 group files
    collect_g0 > "$TMP_DIR/g0" &
    collect_g1 "$CURR_CNT" > "$TMP_DIR/g1" &
    collect_g2 > "$TMP_DIR/g2" &
    
    # Wait for all background reads to complete
    wait
    
    # 2. Parse Group 0 using single-pass pure shell matching
    pmic_c0="ERR"; des0_1a="ERR"; des0_0a="ERR"; des0_0b="ERR"; des0_0c="ERR"
    des0_1dc="ERR"; des0_1fc="ERR"; des0_21c="ERR"; des0_23c="ERR"
    
    while read -r line; do
        case "$line" in
            *"[0x"*"] = 0x"*)
                line_clean="${line%$CR}"
                reg_hex="${line_clean##*\[}"
                reg_num="${reg_hex%%\]*}"
                reg_val="${line_clean##*= }"
                case "${reg_num#0x}" in
                    1) pmic_c0="$reg_val" ;;
                    1a|01a) des0_1a="$reg_val" ;;
                    a|0a) des0_0a="$reg_val" ;;
                    b|0b) des0_0b="$reg_val" ;;
                    c|0c) des0_0c="$reg_val" ;;
                    1dc) des0_1dc="$reg_val" ;;
                    1fc) des0_1fc="$reg_val" ;;
                    21c) des0_21c="$reg_val" ;;
                    23c) des0_23c="$reg_val" ;;
                esac
                ;;
        esac
    done < "$TMP_DIR/g0"

    # 3. Parse Group 1 & Bypass Links (Bypass variables persist across iterations)
    pmic_c1="ERR"; des1_1a="ERR"; des1_0a="ERR"; des1_0b="ERR"; des1_0c="ERR"
    des1_1dc="ERR"; des1_1fc="ERR"; des1_21c="ERR"; des1_23c="ERR"
    
    curr_section="G1"
    
    while read -r line; do
        case "$line" in
            *"--- BYPASS START ---"*)
                curr_section="BYPASS"
                ;;
            *"F2_13:"*)
                curr_section="F2_13"
                ;;
            *"F2_112:"*)
                curr_section="F2_112"
                ;;
            *"F4_13:"*)
                curr_section="F4_13"
                ;;
            *"F4_112:"*)
                curr_section="F4_112"
                ;;
            *"[0x"*"] = 0x"*)
                line_clean="${line%$CR}"
                reg_hex="${line_clean##*\[}"
                reg_num="${reg_hex%%\]*}"
                reg_val="${line_clean##*= }"
                
                if [ "$curr_section" = "G1" ]; then
                    case "${reg_num#0x}" in
                        1) pmic_c1="$reg_val" ;;
                        1a|01a) des1_1a="$reg_val" ;;
                        a|0a) des1_0a="$reg_val" ;;
                        b|0b) des1_0b="$reg_val" ;;
                        c|0c) des1_0c="$reg_val" ;;
                        1dc) des1_1dc="$reg_val" ;;
                        1fc) des1_1fc="$reg_val" ;;
                        21c) des1_21c="$reg_val" ;;
                        23c) des1_23c="$reg_val" ;;
                    esac
                elif [ "$curr_section" = "F2_13" ]; then
                    bp_f2_13="$reg_val"
                elif [ "$curr_section" = "F2_112" ]; then
                    bp_f2_112="$reg_val"
                elif [ "$curr_section" = "F4_13" ]; then
                    bp_f4_13="$reg_val"
                elif [ "$curr_section" = "F4_112" ]; then
                    bp_f4_112="$reg_val"
                fi
                ;;
        esac
    done < "$TMP_DIR/g1"

    # 4. Parse Group 2
    pmic_c2="ERR"; des2_1a="ERR"; des2_0a="ERR"; des2_0b="ERR"; des2_0c="ERR"
    des2_1dc="ERR"; des2_1fc="ERR"; des2_21c="ERR"; des2_23c="ERR"
    
    while read -r line; do
        case "$line" in
            *"[0x"*"] = 0x"*)
                line_clean="${line%$CR}"
                reg_hex="${line_clean##*\[}"
                reg_num="${reg_hex%%\]*}"
                reg_val="${line_clean##*= }"
                case "${reg_num#0x}" in
                    1) pmic_c2="$reg_val" ;;
                    1a|01a) des2_1a="$reg_val" ;;
                    a|0a) des2_0a="$reg_val" ;;
                    b|0b) des2_0b="$reg_val" ;;
                    c|0c) des2_0c="$reg_val" ;;
                    1dc) des2_1dc="$reg_val" ;;
                    1fc) des2_1fc="$reg_val" ;;
                    21c) des2_21c="$reg_val" ;;
                    23c) des2_23c="$reg_val" ;;
                esac
                ;;
        esac
    done < "$TMP_DIR/g2"
    
    # 5. Format strings
    pmic_c0_f=$(format_pmic_ports "$pmic_c0")
    pmic_c1_f=$(format_pmic_ports "$pmic_c1")
    pmic_c2_f=$(format_pmic_ports "$pmic_c2")
    
    des0_p0=$(format_port_status "$des0_1a" "$des0_1dc")
    des0_p1=$(format_port_status "$des0_0a" "$des0_1fc")
    des0_p2=$(format_port_status "$des0_0b" "$des0_21c")
    des0_p3=$(format_port_status "$des0_0c" "$des0_23c")
    
    des1_p0=$(format_port_status "$des1_1a" "$des1_1dc")
    des1_p1=$(format_port_status "$des1_0a" "$des1_1fc")
    des1_p2=$(format_port_status "$des1_0b" "$des1_21c")
    des1_p3=$(format_port_status "$des1_0c" "$des1_23c")
    
    des2_p0=$(format_port_status "$des2_1a" "$des2_1dc")
    des2_p1=$(format_port_status "$des2_0a" "$des2_1fc")
    des2_p2=$(format_port_status "$des2_0b" "$des2_21c")
    des2_p3=$(format_port_status "$des2_0c" "$des2_23c")

    # Format bypass strings
    bp_svs_p3=$(format_bypass_status "$bp_f2_13" "$bp_f2_112")
    bp_svs_p4=$(format_bypass_status "$bp_f4_13" "$bp_f4_112")
    bp_fwc_p1=$(format_bypass_status "$bp_f2_13" "$bp_f2_112")

    # 6. Render double-buffered elegant layout
    printf "%b" "\033[H"
    printf "%b%s%b\n" "$BORDER_COLOR" "$BORDER_TOP" "$RESET_COLOR"
    printf "               \033[1;37mCHERY GMSL & PMIC REAL-TIME MONITOR v1.4\033[0m\n"
    printf "%b%s%b\n" "$BORDER_COLOR" "$BORDER_MID" "$RESET_COLOR"
    printf "  \033[1;37m[ PMIC POWER DIAGNOSTICS - MAX20087 ]\033[0m\n"
    printf "    Core 0 (Bus 0) :  %b\n" "$pmic_c0_f"
    printf "    Core 1 (Bus 1) :  %b\n" "$pmic_c1_f"
    printf "    Core 2 (Bus 2) :  %b\n" "$pmic_c2_f"
    printf "%b%s%b\n" "$BORDER_COLOR" "$BORDER_MID" "$RESET_COLOR"
    printf "  \033[1;37m[ DESERIALIZER LINK & VIDEO LOCK STATUS ]\033[0m\n"
    printf "\n"
    printf "  \033[1;36mGroup 0 周视 (0x4E)\033[0m\n"
    printf "    P0 右后 %b         P1 左后 %b\n" "$des0_p0" "$des0_p1"
    printf "    P2 左前 %b         P3 右前 %b\n" "$des0_p2" "$des0_p3"
    printf "\n"
    printf "  \033[1;36mGroup 1 环视 (0x52)\033[0m\n"
    printf "    P0 后鱼 %b         P1 左鱼 %b\n" "$des1_p0" "$des1_p1"
    printf "    P2 前鱼 %b         P3 右鱼 %b\n" "$des1_p2" "$des1_p3"
    printf "\n"
    printf "  \033[1;36mGroup 2 远雷 (0xD6)\033[0m\n"
    printf "    P0 前远 %b         P1 前广 %b\n" "$des2_p0" "$des2_p1"
    printf "    P2 后中 %b         P3 前雷 %b\n" "$des2_p2" "$des2_p3"
    printf "%b%s%b\n" "$BORDER_COLOR" "$BORDER_MID" "$RESET_COLOR"
    printf "  \033[1;37m[ BYPASS-SERIALIZER  LINK & PCLKDET STATUS ]\033[0m\n"
    printf "\n"
    printf "    SVS PIN3 %b         SVS PIN4 %b\n" "$bp_svs_p3" "$bp_svs_p4"
    printf "    FWC PIN1 %b\n" "$bp_fwc_p1"
    printf "%b%s%b\n" "$BORDER_COLOR" "$BORDER_BOT" "$RESET_COLOR"
    printf "  Legend: LKD=Locked  DISC=Lost  |  OK=Video OK  NV=Video Lost\n"
    printf "  Status: Active | Interval: %ss | Press Ctrl+C to exit.\n" "$INTERVAL"
    # Clear any residual screen text down below
    printf "%b" "\033[J"
    
    sleep "$INTERVAL"
done