#!/usr/bin/env bash
# Project-local Android/JDK environment, matching hermes-client-mobile.

if [[ -z "${JAVA_HOME:-}" ]]; then
  if [[ -x /usr/libexec/java_home ]]; then
    JAVA_HOME="$(/usr/libexec/java_home -v 17 2>/dev/null || true)"
  fi
  if [[ -z "$JAVA_HOME" ]] && command -v brew >/dev/null 2>&1; then
    BREW_JDK_PREFIX="$(brew --prefix openjdk@17 2>/dev/null || true)"
    if [[ -n "$BREW_JDK_PREFIX" ]]; then
      JAVA_HOME="$BREW_JDK_PREFIX/libexec/openjdk.jdk/Contents/Home"
    fi
  fi
fi
: "${JAVA_HOME:?未找到 JDK 17，请设置 JAVA_HOME}"

if [[ -z "${ANDROID_HOME:-}" ]]; then
  ANDROID_HOME="${ANDROID_SDK_ROOT:-}"
fi
if [[ -z "$ANDROID_HOME" ]] && command -v brew >/dev/null 2>&1; then
  ANDROID_HOME="$(brew --prefix android-commandlinetools 2>/dev/null || true)"
fi
if [[ -z "$ANDROID_HOME" ]]; then
  ANDROID_HOME="$HOME/Library/Android/sdk"
fi
: "${ANDROID_HOME:?未找到 Android SDK，请设置 ANDROID_HOME}"

NDK_VERSION="${ZCHARLIE_NDK_VERSION:-28.0.13004108}"
NDK_HOME="${NDK_HOME:-$ANDROID_HOME/ndk/$NDK_VERSION}"
export JAVA_HOME ANDROID_HOME ANDROID_SDK_ROOT="$ANDROID_HOME" NDK_HOME
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/emulator:$PATH"
