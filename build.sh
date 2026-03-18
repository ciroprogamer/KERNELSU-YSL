#!/usr/bin/env bash

#
# SPDX-License-Identifier: Apache-2.0 license
#

# Toolchains Path
TC_PATH="$HOME/toolchains"

# sworkflow
export SW_SRC_DIR="$TC_PATH/sworkflow"

# Toolchain paths
export PATH="$TC_PATH/clang/bin:$TC_PATH/gcc64/bin:$TC_PATH/gcc32/bin:$SW_SRC_DIR:$PATH"

# Check if sworkflow is available
if [[ ! -f "$SW_SRC_DIR/sw" ]]; then
    echo "sworkflow not found at $SW_SRC_DIR"
    echo "Fix: git clone https://github.com/danascape/sworkflow $SW_SRC_DIR"
    exit 1
fi

# sworkflow expects to be at $HOME/sworkflow, create symlink if needed
if [[ ! -e "$HOME/sworkflow" ]]; then
    echo "Creating sworkflow symlink..."
    ln -s "$SW_SRC_DIR" "$HOME/sworkflow"
fi

SW="$SW_SRC_DIR/sw"

# Workspace
WORKSPACE_PATH="$(pwd)"
DEVICE="ysl"
LOG_FILE="$WORKSPACE_PATH/build.log"

set_build_status() {
    local status="$2"
    local device="$1"
    if [[ "$status" == "pass" ]]; then
        STATUS="Passing"
    elif [[ "$status" == "fail" ]]; then
        STATUS="Failed"
    else
        STATUS="Unknown"
    fi
    genJSON "$device"
    unset STATUS
}

genJSON() {
    local DEVICE=$1
    END=$(date +"%s")
    DIFF=$(($END - $START))
    echo "Generating build info"
    TIME="$DIFF seconds"
    {
        echo ""
        echo "============================================"
        echo "  Device  : $DEVICE"
        echo "  Status  : $STATUS"
        echo "  Time    : $TIME"
        echo "  Log     : $LOG_FILE"
        echo "============================================"
    } | tee -a "$LOG_FILE"
}

check_kernel_image() {
    if [[ -f "$WORKSPACE_PATH/out/arch/arm64/boot/Image.gz-dtb" ]]; then
        echo "true"
    else
        echo "false"
    fi
}

kernel_build() {
    : > "$LOG_FILE"
    START=$(date +"%s")

    echo "Starting build for $DEVICE" | tee -a "$LOG_FILE"

    if [[ "$1" == "--clean" ]]; then
        echo "Cleaning kernel tree..." | tee -a "$LOG_FILE"
        make -C "$WORKSPACE_PATH" O="$WORKSPACE_PATH/out" clean 2>&1 | tee -a "$LOG_FILE"
        make -C "$WORKSPACE_PATH" O="$WORKSPACE_PATH/out" mrproper 2>&1 | tee -a "$LOG_FILE"
    fi

    bash "$SW" b "$DEVICE" 2>&1 | tee -a "$LOG_FILE"

    buildStatus=$(check_kernel_image)
    buildStatus=$(echo "$buildStatus" | tr -d '[:space:]')

    case "$buildStatus" in
        true)  set_build_status "$DEVICE" pass ;;
        false) set_build_status "$DEVICE" fail; exit 1 ;;
        *)     echo "error: Unknown status" | tee -a "$LOG_FILE"; exit 1 ;;
    esac
}

kernel_build "$@"
